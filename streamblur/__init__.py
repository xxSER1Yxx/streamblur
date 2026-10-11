"""StreamBlur — автоблюр фильма в OBS по таймингам."""
import os

__version__ = "0.1.0"

# Страница проекта: куда писать о проблемах и где поддержать.
# Адрес репозитория: https://github.com/xxSER1Yxx/streamblur
# Переменная окружения STREAMBLUR_REPO позволяет переопределить его без правки кода.
#    Ту же ссылку нужно вписать в packaging/installer.iss (#define MyAppURL) —
#    автоматически она туда не попадёт, это два разных файла.
__repo_url__ = os.environ.get(
    "STREAMBLUR_REPO", "https://github.com/xxSER1Yxx/streamblur"
)

__all__ = ["__version__", "__repo_url__"]
