"""Runs every check and writes everything to report.txt.

Run:  python report.py
Then send me the report.txt file it creates in this folder.
"""

import io
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr

import config


def main():
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        print("=== settings in use ===")
        for name in sorted(dir(config)):
            if name.isupper():
                value = getattr(config, name)
                if "SECRET" in name:
                    value = "(set)" if value else "(not set)"
                print(f"{name} = {value!r}")

        print("\n=== python ===")
        print(sys.version)
        for module in ("flask", "msal", "requests"):
            try:
                mod = __import__(module)
                print(f"{module}: {getattr(mod, '__version__', 'installed')}")
            except Exception as exc:
                print(f"{module}: NOT INSTALLED ({exc})")

        print("\n=== diagnostics ===")
        try:
            import diagnose

            diagnose.main()
        except Exception:
            traceback.print_exc()

        print("\n=== what the dashboard would show ===")
        try:
            import dataverse

            token = dataverse.get_token()
            rows = dataverse.fetch_service_requests(token)
            records = dataverse.classify(rows)
            print(f"rows after filters: {len(records)}")
            for note in dataverse.NOTES:
                print("NOTE:", note)
            for record in records[:10]:
                print(record)
        except Exception:
            traceback.print_exc()

    text = buffer.getvalue()
    with open("report.txt", "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print("\n-----------------------------------------")
    print("Saved to report.txt - send me that file.")


if __name__ == "__main__":
    main()
