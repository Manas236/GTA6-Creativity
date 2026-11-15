"""Run connection-test network requests outside the Flask process."""

import json
import sys

import requests


def main():
    request = json.load(sys.stdin)
    if request["operation"] == "telegram":
        import connection_checks
        result = connection_checks.send_telegram_test(request["env"], request["cfg"])
    else:
        with requests.request(request["method"], request["url"], **request["kwargs"]) as response:
            body = bytearray()
            for chunk in response.iter_content(4096):
                body.extend(chunk)
                if len(body) > 65536:
                    raise ValueError("API response exceeded 64 KiB")
            result = {"status": response.status_code, "text": body.decode("utf-8", errors="replace")}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
