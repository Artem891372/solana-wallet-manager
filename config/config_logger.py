import logging

def get_logger(filename:str) -> logging.Logger:
    # Настройка фильтра для подавления спама логов от stem
    filepath="logs/"+filename
    class StemLogFilter(logging.Filter):
        def filter(self, record):
            # Фильтруем сообщения SocketClosed и peek of closed file
            if "SocketClosed" in record.getMessage() or "peek of closed file" in record.getMessage():
                return False
            return True

    # Настройка логирования
    logging.basicConfig(
        filename=filepath,
        filemode='a',
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    # Добавляем фильтр к логгеру stem
    stem_logger = logging.getLogger('stem')
    stem_logger.addFilter(StemLogFilter())
    # Понижаем уровень логов stem до DEBUG, чтобы они не попадали в INFO
    stem_logger.setLevel(logging.DEBUG)
    return logger