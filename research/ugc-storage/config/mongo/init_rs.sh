#!/usr/bin/env bash
# Инициализация трёх replica set'ов: config-набор и два шарда.
#
# Отдельным шагом от init_cluster.js, потому что mongos не стартует, пока
# config-набор не инициирован, а `sh.addShard` без mongos невозможен.
set -euo pipefail

init_rs() {
    local rs_name="$1"
    local host_1="$2"
    local host_2="$3"
    local host_3="$4"
    local extra="$5" # configsvr: true — только для config-набора

    echo "==> ${rs_name}: rs.initiate"
    mongosh --quiet --host "${host_1}" --port 27017 --eval "
        try {
            rs.status();
            print('${rs_name}: уже инициирован, пропускаем');
        } catch (e) {
            rs.initiate({
                _id: '${rs_name}',
                ${extra}
                members: [
                    {_id: 0, host: '${host_1}:27017'},
                    {_id: 1, host: '${host_2}:27017'},
                    {_id: 2, host: '${host_3}:27017'}
                ]
            });
        }
    "

    echo "==> ${rs_name}: ждём выбора PRIMARY"
    # Спрашиваем НЕ «primary ли ты», а «есть ли PRIMARY в наборе»: выборы
    # выигрывает любой из трёх узлов, и проверка isWritablePrimary на первом
    # давала ложное «набор не поднялся» в двух случаях из трёх.
    #
    # Запас — пять минут: на пустом наборе выборы занимают секунды, но после
    # перезапуска узлов с данными сначала идёт восстановление. Без ожидания
    # следующий шаг падает на NotWritablePrimary.
    for _ in $(seq 1 150); do
        if mongosh --quiet --host "${host_1}" --port 27017 \
            --eval "rs.status().members.some(m => m.stateStr === 'PRIMARY')" 2>/dev/null | grep -q true; then
            echo "==> ${rs_name}: PRIMARY готов"
            return 0
        fi
        sleep 2
    done
    echo "!!! ${rs_name}: PRIMARY не появился за 300 с" >&2
    return 1
}

init_rs 'cfgrs' 'mongo-cfg-01' 'mongo-cfg-02' 'mongo-cfg-03' 'configsvr: true,'
init_rs 'sh1rs' 'mongo-sh1-01' 'mongo-sh1-02' 'mongo-sh1-03' ''
init_rs 'sh2rs' 'mongo-sh2-01' 'mongo-sh2-02' 'mongo-sh2-03' ''

echo '==> Все replica set готовы'
