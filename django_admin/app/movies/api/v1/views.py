from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Q, Value
from django.http import JsonResponse
from django.views.generic.detail import BaseDetailView
from django.views.generic.list import BaseListView

from movies.models import FilmWork, Roles


class MoviesApiMixin:
    model = FilmWork
    http_method_names = ['get']

    def get_queryset(self):
        return (
            FilmWork.objects.prefetch_related(
                'genres',
                'persons',
            )
            .values(
                'id',
                'title',
                'description',
                'creation_date',
                'rating',
                'type',
            )
            .annotate(
                genres=ArrayAgg('genres__name', distinct=True, default=Value([])),
                actors=ArrayAgg(
                    'persons__full_name',
                    filter=Q(personfilmwork__role=Roles.ACTOR),
                    distinct=True,
                    default=Value([]),
                ),
                directors=ArrayAgg(
                    'persons__full_name',
                    filter=Q(personfilmwork__role=Roles.DIRECTOR),
                    distinct=True,
                    default=Value([]),
                ),
                writers=ArrayAgg(
                    'persons__full_name',
                    filter=Q(personfilmwork__role=Roles.WRITER),
                    distinct=True,
                    default=Value([]),
                ),
            )
        )

    def render_to_response(self, context, **response_kwargs):
        return JsonResponse(context)


class MoviesListApi(MoviesApiMixin, BaseListView):
    paginate_by = 50

    def get_context_data(self, *, object_list=None, **kwargs):
        queryset = self.get_queryset()
        paginator, page, queryset, _is_paginated = self.paginate_queryset(queryset, self.paginate_by)
        context = {
            'count': paginator.count,
            'prev': page.previous_page_number() if page.has_previous() else None,
            'results': list(queryset),
            'next': page.next_page_number() if page.has_next() else None,
            'total_pages': paginator.num_pages,
        }
        return context


class MoviesDetailApi(MoviesApiMixin, BaseDetailView):
    def get_context_data(self, **kwargs):
        return kwargs.get('object', {})
