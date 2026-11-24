"""Load the mock data into the DynamoDB tables.

Run from the backend folder:

    python -m scripts.seed_dynamodb --dry-run     show what would be written, change nothing
    python -m scripts.seed_dynamodb               write the data

Safe to repeat: items are overwritten and nothing is ever deleted. Only the six tables named
<DYNAMODB_TABLE_PREFIX>customers, network-areas, tickets, calls, packs and gp-centres are touched.
The calls table is left empty on purpose: the app writes to it.
AWS credentials, region and the table prefix come from config/.env.
The items are built by copilot/repo/seed.py, the same code the local-file fallback uses.
"""

import argparse
import sys
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from config.settings import get_settings
from copilot.repo.seed import TABLES, build_seed
from copilot.utils.aws import aws_session


def check_tables(client, prefix: str) -> list[str]:
    """Every table must exist with the expected keys. Returns the problems found."""
    problems = []
    for short, (pk, sk) in TABLES.items():
        name = prefix + short
        try:
            table = client.describe_table(TableName=name)["Table"]
        except client.exceptions.ResourceNotFoundException:
            problems.append(f"{name}: table not found in this account and region")
            continue
        keys = {k["KeyType"]: k["AttributeName"] for k in table["KeySchema"]}
        if keys.get("HASH") != pk or keys.get("RANGE") != sk:
            problems.append(f"{name}: keys are {keys}, expected partition={pk} sort={sk}")
    return problems


def write_all(resource, prefix: str, seed: dict[str, list[dict]]) -> None:
    for short, rows in seed.items():
        pk, sk = TABLES[short]
        table = resource.Table(prefix + short)
        with table.batch_writer(overwrite_by_pkeys=[k for k in (pk, sk) if k]) as batch:
            for row in rows:
                batch.put_item(Item=row)


def read_back(resource, prefix: str, seed: dict[str, list[dict]]) -> list[str]:
    """Read every written item again (strongly consistent) and compare it with what was written."""
    bad = []
    for short, rows in seed.items():
        pk, sk = TABLES[short]
        table = resource.Table(prefix + short)
        for row in rows:
            key = {pk: row[pk], **({sk: row[sk]} if sk else {})}
            if table.get_item(Key=key, ConsistentRead=True).get("Item") != row:
                bad.append(f"{prefix + short}: {key}")
    return bad


def summary(seed: dict[str, list[dict]], prefix: str) -> list[str]:
    lines = [f"  {prefix + short:34} {len(rows):3} items" for short, rows in seed.items()]
    lines.append(f"  {prefix + 'calls':34}   0 items (written by the app)")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show what would be written, change nothing")
    args = parser.parse_args()

    settings = get_settings()
    prefix = settings.dynamodb_table_prefix
    seed = build_seed(settings, datetime.now(timezone.utc))
    print("Seed data:")
    print("\n".join(summary(seed, prefix)))
    if args.dry_run:
        print("Dry run: nothing was written.")
        return

    session = aws_session(settings)
    try:
        who = session.client("sts").get_caller_identity()
        print(f"Account {who['Account']}, region {session.region_name}")
        problems = check_tables(session.client("dynamodb"), prefix)
        if problems:
            print("Not writing anything. Fix these first:\n  " + "\n  ".join(problems))
            sys.exit(1)
        resource = session.resource("dynamodb")
        write_all(resource, prefix, seed)
        bad = read_back(resource, prefix, seed)
    except (ClientError, BotoCoreError) as error:
        print(f"AWS error: {type(error).__name__}: {str(error)[:300]}")
        print("If the credentials expired, refresh the AWS_* values in config/.env and run again.")
        sys.exit(1)

    total = sum(len(rows) for rows in seed.values())
    if bad:
        print(f"Wrote {total} items, but {len(bad)} did not read back identically:\n  " + "\n  ".join(bad))
        sys.exit(1)
    print(f"Wrote {total} items and read every one back: all match.")


if __name__ == "__main__":
    main()
