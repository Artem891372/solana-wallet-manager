import os
import json

def load_config_json(file_path="config.json", module=None) -> dict:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Конфиг-файл {file_path} не найден")
    with open(file_path, 'r') as file:
        config = json.load(file)
    return config[module]