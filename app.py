# app.py — entry point
# Imports directly from app_factory so a blank __init__.py never causes issues
from oiapp.app_factory import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=True, use_reloader=False)
