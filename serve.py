"""Production entry point for the SLA Monitor dashboard."""

from waitress import serve

import app
import config


def main():
    app.initialize_monitor()
    print(
        f"SLA monitor ready - open http://{config.HOST}:{config.PORT}",
        flush=True,
    )
    serve(
        app.app,
        host=config.HOST,
        port=config.PORT,
        threads=8,
        channel_timeout=120,
    )


if __name__ == "__main__":
    main()