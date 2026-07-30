class NotAllowedTable(Exception):
    """Exception raised for the wrong table name or unsupported entity"""

    def __init__(self, table_name: str):
        super().__init__(f'Table {table_name} not allowed')
