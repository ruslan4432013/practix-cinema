// Регистрация шардов, шардирование коллекций, индексы и валидаторы.
//
// Запускается одноразовым job'ом mongo-init против mongos — по образцу
// clickhouse-init/kafka-init в основном стеке проекта. Скрипт идемпотентен:
// повторный запуск на живом кластере ничего не ломает.

const DB_NAME = 'ugc';

// --- 1. Шарды ---------------------------------------------------------------
const SHARDS = [
    'sh1rs/mongo-sh1-01:27017,mongo-sh1-02:27017,mongo-sh1-03:27017',
    'sh2rs/mongo-sh2-01:27017,mongo-sh2-02:27017,mongo-sh2-03:27017',
];

for (const conn of SHARDS) {
    try {
        sh.addShard(conn);
        print('addShard: ' + conn);
    } catch (e) {
        print('addShard пропущен (' + conn + '): ' + e.message);
    }
}

try {
    sh.enableSharding(DB_NAME);
} catch (e) {
    print('enableSharding пропущен: ' + e.message);
}

const db_ = db.getSiblingDB(DB_NAME);

// --- 2. Схемы коллекций -----------------------------------------------------
// В BSON нет smallint: целые — int32 или int64. Требование «заложить хранение
// smallint» здесь выражается не типом, а валидатором: оценка обязана быть целым
// числом 0..10. Диапазон проверяет база, а не только приложение.
function ensureCollection(name, validator) {
    if (!db_.getCollectionNames().includes(name)) {
        db_.createCollection(name, validator ? {validator: validator} : {});
        print('createCollection: ' + name);
    } else if (validator) {
        db_.runCommand({collMod: name, validator: validator});
    }
}

const ratingField = {bsonType: 'int', minimum: 0, maximum: 10};

ensureCollection('likes', {
    $jsonSchema: {
        bsonType: 'object',
        required: ['user_id', 'film_id', 'rating'],
        properties: {
            user_id: {bsonType: 'string'},
            film_id: {bsonType: 'string'},
            rating: ratingField,
        },
    },
});
ensureCollection('bookmarks');
ensureCollection('reviews');
ensureCollection('review_votes');
ensureCollection('film_rating');

// --- 3. Индексы -------------------------------------------------------------
// Индексы создаются ДО shardCollection: unique-индекс, которым подпирается
// ключ шардирования, обязан существовать заранее.
db_.likes.createIndex({film_id: 1, user_id: 1}, {unique: true, name: 'likes_film_user_uniq'});
db_.likes.createIndex({user_id: 1, rating: -1, updated_at: -1}, {name: 'likes_user_rating'});
// Индекс под агрегацию по фильму — и он обязан начинаться с ПОЛНОГО ключа
// шардирования, а не только с film_id.
//
// Это неочевидно и стоило исследованию отдельного разбора. Ключ шардирования
// {film_id, user_id} отбирает нужные документы, но оценки в нём нет, и MongoDB
// лезет за каждым документом на диск. Напрашивающийся {film_id, rating} проблему
// НЕ решает: в шардированной коллекции план содержит стадию SHARDING_FILTER,
// которая проверяет, принадлежит ли документ этому шарду, — а проверить это по
// индексу без user_id нельзя, и FETCH остаётся обязательным.
//
// С {film_id, user_id, rating} план становится PROJECTION_COVERED. На фильме со
// 160 тысячами оценок разница измерена: 3378 мс против 61 мс.
db_.likes.createIndex({film_id: 1, user_id: 1, rating: 1}, {name: 'likes_film_user_rating'});

db_.bookmarks.createIndex({user_id: 1, film_id: 1}, {unique: true, name: 'bookmarks_user_film_uniq'});
db_.bookmarks.createIndex({user_id: 1, created_at: -1}, {name: 'bookmarks_user_created'});

// Правило ESR (Equality, Sort, Range): film_id — равенство, второе поле —
// сортировка. Три индекса — три поддерживаемых порядка сортировки рецензий;
// алгоритм ранжирования ещё будет меняться, и добавление четвёртого порядка
// стоит ровно одного индекса, а не переезда схемы.
db_.reviews.createIndex({film_id: 1, _id: 1}, {unique: true, name: 'reviews_film_id_uniq'});
db_.reviews.createIndex({film_id: 1, created_at: -1}, {name: 'reviews_film_created'});
db_.reviews.createIndex({film_id: 1, useful_score: -1}, {name: 'reviews_film_useful'});
db_.reviews.createIndex({film_id: 1, author_rating: -1}, {name: 'reviews_film_author_rating'});

db_.review_votes.createIndex({review_id: 1, user_id: 1}, {unique: true, name: 'review_votes_uniq'});

// --- 4. Шардирование --------------------------------------------------------
// Ключи — составные ranged, а не hashed. Причина техническая: unique-индекс в
// шардированной коллекции обязан начинаться с ключа шардирования, а hashed-ключ
// такой индекс подпирать не может. Ranged-ключ по film_id даёт и уникальность
// пары (film_id, user_id), и targeted-запросы по фильму.
//
// film_rating — исключение: там ключ шардирования совпадает с _id, любая
// операция по _id targeted по определению, поэтому hashed безопасен и даёт
// лучшее распределение.
function shardCollection(name, key, unique) {
    try {
        sh.shardCollection(DB_NAME + '.' + name, key, unique === true);
        print('shardCollection: ' + name + ' ' + JSON.stringify(key));
    } catch (e) {
        print('shardCollection пропущен (' + name + '): ' + e.message);
    }
}

shardCollection('likes', {film_id: 1, user_id: 1}, true);
shardCollection('bookmarks', {user_id: 1, film_id: 1}, true);
shardCollection('reviews', {film_id: 1, _id: 1}, true);
shardCollection('review_votes', {review_id: 1, user_id: 1}, true);
shardCollection('film_rating', {_id: 'hashed'});

// --- 5. Пресплит ------------------------------------------------------------
// Без него вся заливка уходит в один шард, а балансировщик растаскивает чанки
// уже потом — часами и конкурируя с записью. Идентификаторы — hex-строки с
// равномерным первым символом, поэтому 16 границ дают ровное распределение
// сразу. Ключи в исследовании намеренно не коррелируют с популярностью
// (см. lib/dataset.py), иначе ranged-ключ создал бы искусственный горячий шард.
const HEX = '0123456789abcdef'.split('');
const shardNames = db.getSiblingDB('config').shards.find().toArray().map((s) => s._id).sort();

// Разрезание и раскладка — двумя отдельными проходами, а не «разрезал — сдвинул»
// по каждой границе. Причина: moveChunk перемещает чанк, СОДЕРЖАЩИЙ точку,
// поэтому при чередовании двигается всякий раз хвостовой чанк, и итог зависит от
// того, где этот хвост оказался на предыдущем шаге. Раскладка уже готового
// списка чанков по кругу идемпотентна и от истории запусков не зависит.
function presplit(name, keyFields) {
    const ns = DB_NAME + '.' + name;
    if (shardNames.length < 2) {
        print('presplit пропущен (' + name + '): шардов меньше двух');
        return;
    }

    HEX.forEach((prefix, i) => {
        if (i === 0) {
            return; // первый чанк начинается от MinKey, границу ставить не нужно
        }
        const boundary = {};
        keyFields.forEach((field, idx) => {
            boundary[field] = idx === 0 ? prefix + '0000000-0000-0000-0000-000000000000' : MinKey;
        });
        try {
            sh.splitAt(ns, boundary);
        } catch (e) {
            // «is a boundary key of existing chunk» означает, что здесь уже
            // разрезано на прошлом запуске. Это не ошибка.
            if (e.message.indexOf('boundary key of existing chunk') < 0) {
                print('split ' + ns + ' @' + prefix + ': ' + e.message);
            }
        }
    });

    const collectionUuid = db.getSiblingDB('config').collections.findOne({_id: ns}).uuid;
    const chunks = db.getSiblingDB('config').chunks.find({uuid: collectionUuid}).sort({min: 1}).toArray();

    let moved = 0;
    chunks.forEach((chunk, i) => {
        const target = shardNames[i % shardNames.length];
        if (chunk.shard === target) {
            return;
        }
        // ExceededTimeLimit из-за незавершённой уборки «сирот» после предыдущей
        // миграции — штатная гонка на пустых чанках, лечится повтором.
        for (let attempt = 0; attempt < 3; attempt++) {
            try {
                sh.moveChunk(ns, chunk.min, target);
                moved++;
                return;
            } catch (e) {
                if (attempt === 2) {
                    print('moveChunk ' + ns + ' -> ' + target + ': ' + e.message);
                }
                sleep(2000);
            }
        }
    });
    print('presplit готов: ' + name + ' — чанков ' + chunks.length + ', перемещено ' + moved);
}

presplit('likes', ['film_id', 'user_id']);
presplit('bookmarks', ['user_id', 'film_id']);
presplit('reviews', ['film_id', '_id']);
presplit('review_votes', ['review_id', 'user_id']);

print('==> Кластер готов');
