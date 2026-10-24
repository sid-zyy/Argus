"""Argus Connect: reusable adapters between Argus and the services it looks after.

A connected service is a folder with an argus.yaml (see scenarios/quickbite_orders/argus.yaml). It names
- a repo adapter (git) for the code and its history,
- an alert adapter (alertmanager, grafana, standard) that turns webhooks into the standard IncidentInput,
- log adapters (text, jsonl, csv, command) that render any log format as the numbered lines agents read,
- the commands that reproduce the failure and run the tests, its dependencies, and its severity/approval policy.

The agents and the pipeline are the same for every service; only the config and adapters change.
    python -m connect.wizard list | check <service> [--full] | new | route <payload.json> | schema
"""
