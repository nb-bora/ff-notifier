"""Logger applicatif (format JSON + réduction du bruit PDF).

Ce module configure `logging` pour:
- écrire en stdout en JSON-lines (facile à ingérer par CloudWatch/ELK)
- réduire le bruit de dépendances PDF (WeasyPrint, PIL, fontTools) par défaut

Effets de bord:
- configure le root logger via `logging.basicConfig(...)`.
"""

import logging
import os
import sys

log_level = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

logger = logging.getLogger("ff-notifier")

_VERBOSE_WEASY = os.getenv("VERBOSE_WEASYPRINT", "false").lower() == "true"

if not _VERBOSE_WEASY:

    class _DropFontTools(logging.Filter):
        """Filtre qui supprime les logs `fontTools*` (souvent très verbeux)."""

        def filter(self, record: logging.LogRecord) -> bool:
            n = record.name
            return n != "fontTools" and not n.startswith("fontTools.")

    _f = _DropFontTools()
    for _h in logging.root.handlers:
        _h.addFilter(_f)

    for _name in (
        "fontTools",
        "fontTools.subset",
        "fontTools.ttLib",
        "fontTools.ttLib.ttFont",
        "weasyprint",
        "PIL",
        "PIL.PngImagePlugin",
    ):
        logging.getLogger(_name).setLevel(logging.ERROR)
