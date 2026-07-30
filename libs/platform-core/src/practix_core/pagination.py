"""Параметры постраничной выдачи, общие для публичных API.

Класс был побайтово одинаковым в ``rest/api/v1/dependencies.py`` и
``auth/src/api/v1/dependencies.py`` — включая одинаковые русские ``description``.
Расхождение здесь заметили бы не сразу и не в коде, а в документации: у двух
сервисов молча разъехались бы границы ``page_size`` в Swagger.
"""

from fastapi import Query


class PaginationParams:
    """Зависимость FastAPI: номер страницы и её размер.

    Используется как ``pagination: PaginationParams = Depends()``.
    """

    def __init__(
        self,
        page_number: int = Query(1, ge=1, description='Номер страницы'),
        page_size: int = Query(50, ge=1, le=100, description='Размер страницы'),
    ):
        self.page_number = page_number
        self.page_size = page_size
