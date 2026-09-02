"""Run one typed CogniStore Ask query against a REST API."""

from __future__ import annotations

import os

from cognistore.sdk import AskRequest, CogniStoreClient


def main() -> None:
    """Query the configured server and print the typed response as JSON."""

    base_url = os.environ.get("COGNISTORE_URL", "http://127.0.0.1:8080")
    request = AskRequest(
        text="Which documents describe storage lifecycle policy?",
        limit=3,
    )

    with CogniStoreClient(base_url) as client:
        response = client.ask(request)

    print(response.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
