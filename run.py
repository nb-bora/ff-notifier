"""Entrypoint dev local (uvicorn + reload).

Ce fichier sert surtout à lancer facilement le service en local:

```powershell
python run.py
```

En production/containers, on démarre plutôt `uvicorn main:app --app-dir src ...`.
"""

import uvicorn


def main() -> None:
    """Démarre uvicorn en mode reload (dev)."""
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        app_dir="src",
    )


if __name__ == "__main__":
    main()
