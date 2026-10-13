"""Demo-grade weekly model rate cards. Standard library only; prices are USD/1M tokens."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
FIELDS = ("account", "deployment", "model", "version", "sku", "region")
RATES = ("input", "cachedInput", "output")
ARM = "https://management.azure.com"
API_VERSION = "2024-10-01"
LIMIT = 4096


class PricingError(ValueError):
    """An unsafe or incomplete price list must not be published."""


def load(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise PricingError(f"Timestamp must include a timezone: {value}")
    return result


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise PricingError(f"Invalid price: {value}") from error
    if not result.is_finite() or result < 0:
        raise PricingError(f"Invalid price: {value}")
    return result


def text(value):
    return format(number(value), "f")


def normalize(item):
    units = {"1K": 1000, "1M": 1, "1K Tokens": 1000, "1M Tokens": 1,
             "1,000 Tokens": 1000, "1,000,000 Tokens": 1}
    unit = item["unitOfMeasure"]
    if unit not in units:
        raise PricingError(f"Unsupported token unit: {unit}")
    return text(number(item["retailPrice"]) * units[unit])


def key(deployment):
    for field in FIELDS:
        value = deployment.get(field)
        if not isinstance(value, str) or not value or any(c in value for c in "|;=\r\n"):
            raise PricingError(f"Invalid deployment field {field}: {value!r}")
    if deployment["deployment"].lower() == "__pricingversion":
        raise PricingError("Reserved deployment name: __pricingVersion")
    return "|".join(deployment[field] for field in FIELDS)


def az(*args):
    # Azure CLI is a .cmd on Windows; an argument list is used throughout, not a shell.
    executable = "az.cmd" if os.name == "nt" else "az"
    result = subprocess.run([executable, *args, "--output", "json", "--only-show-errors"],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def resource_id(value, kind):
    pattern = (r"/subscriptions/[^/?#\s]+/resourceGroups/[^/?#\s]+/providers/"
               + re.escape(kind) + r"/[^/?#\s]+")
    if not isinstance(value, str) or re.fullmatch(pattern, value, re.IGNORECASE) is None:
        raise PricingError(f"Expected a full {kind} resource ID, got {value!r}")
    return value


def arm_pages(url):
    rows = []
    while url:
        if not url.startswith(ARM + "/"):
            raise PricingError("Unexpected ARM pagination host")
        page = az("rest", "--method", "get", "--url", url)
        rows.extend(page["value"])
        url = page.get("nextLink")
    return rows


def discover(accounts, exclusions):
    if not isinstance(accounts, list) or not accounts or len(set(accounts)) != len(accounts):
        raise PricingError("Accounts must be a nonempty JSON array of unique ARM account IDs")
    rows = []
    for account in accounts:
        account = resource_id(account, "Microsoft.CognitiveServices/accounts")
        details = az("rest", "--method", "get", "--url",
                     f"{ARM}{account}?api-version={API_VERSION}")
        if details["kind"] not in ("OpenAI", "AIServices"):
            raise PricingError(f"Not a Foundry/OpenAI account: {account}")
        for item in arm_pages(f"{ARM}{account}/deployments?api-version={API_VERSION}"):
            model = item["properties"]["model"]
            row = dict(account=account, deployment=item["name"], model=model["name"],
                       version=model["version"], sku=item["sku"]["name"],
                       region=details["location"].lower())
            key(row)
            if any(matches(row, rule) for rule in exclusions):
                print(f"Explicitly excluded non-token deployment: {key(row)}")
                continue
            rows.append(row)
    if not rows:
        raise PricingError("No in-scope token deployments discovered")
    return sorted(rows, key=key)


def retail_page(url, attempts=3):
    # The public price API occasionally resets connections or throttles; retry only transient failures.
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(url, timeout=90) as response:
                return json.load(response)
        except HTTPError as error:
            if (error.code != 429 and error.code < 500) or attempt == attempts:
                raise PricingError(f"Retail price API returned HTTP {error.code}") from error
        except (URLError, ConnectionError, TimeoutError) as error:
            if attempt == attempts:
                raise PricingError(f"Retail price API unreachable after {attempts} attempts: {error}") from error
        time.sleep(2 ** attempt)


def fetch(deployments):
    rows = []
    # The service name has changed over time. Match both historical and current names.
    for region in sorted({row["region"] for row in deployments}):
        region = region.replace("'", "''")
        query = (f"armRegionName eq '{region}' and "
                 "(serviceName eq 'Foundry Models' or serviceName eq 'Cognitive Services')")
        url = "https://prices.azure.com/api/retail/prices?" + urlencode(
            {"currencyCode": "'USD'", "$filter": query})
        while url:
            parsed = urlparse(url)
            if (parsed.scheme != "https" or parsed.hostname != "prices.azure.com"
                    or parsed.port not in (None, 443)):
                raise PricingError("Unexpected retail pagination host")
            page = retail_page(url)
            rows.extend(page["Items"])
            url = page.get("NextPageLink")
    return rows


def matches(deployment, selector):
    if not selector or not set(selector).issubset(FIELDS):
        raise PricingError(f"Invalid deployment selector: {selector}")
    return all(expected == "*" or deployment[field].casefold() == str(expected).casefold()
               for field, expected in selector.items())


def rule_for(deployment, rules):
    candidates = [rule for rule in rules if matches(deployment, rule["match"])]
    if not candidates:
        return None
    specificity = lambda rule: sum(v != "*" for v in rule["match"].values())
    best = max(specificity(rule) for rule in candidates)
    candidates = [rule for rule in candidates if specificity(rule) == best]
    if len(candidates) != 1:
        raise PricingError(f"Ambiguous override/mapping for {key(deployment)}")
    return candidates[0]


def retail_rate(selector, deployment, items, as_of):
    allowed = {"meterId", "meterName", "productName", "skuName", "armSkuName"}
    if not selector or not set(selector).issubset(allowed):
        raise PricingError(f"Invalid explicit meter selector: {selector}")
    candidates = [
        item for item in items
        if item.get("currencyCode") == "USD"
        and item.get("type") == "Consumption"
        and item.get("armRegionName", "").casefold() == deployment["region"].casefold()
        and number(item.get("tierMinimumUnits", 0)) == 0
        and instant(item["effectiveStartDate"]) <= instant(as_of)
        and all(item.get(field, "").casefold() == value.casefold()
                for field, value in selector.items())
    ]
    if not candidates:
        raise PricingError(f"Missing meter {selector} in {key(deployment)}")
    latest = max(instant(item["effectiveStartDate"]) for item in candidates)
    candidates = [item for item in candidates if instant(item["effectiveStartDate"]) == latest]
    # Identical rows occasionally occur in paged snapshots. Different meter IDs are not interchangeable.
    unique = {digest(item): item for item in candidates}
    if len(unique) != 1:
        raise PricingError(f"Ambiguous meter {selector} in {key(deployment)}")
    item = next(iter(unique.values()))
    return normalize(item), item


def resolve(deployment, config, items, as_of):
    provisioned = "provisioned" in deployment["sku"].lower()
    rule = rule_for(deployment, config.get("allocations" if provisioned else "prices", []))
    if provisioned and rule is None:
        raise PricingError(f"PTU requires an explicit internal allocation: {key(deployment)}")
    if rule is not None:
        rates = {name: text(rule["usdPerMillion"][name]) for name in RATES}
        source = "allocation" if provisioned else "override"
        evidence = rule
    else:
        mapping = rule_for(deployment, config.get("meterMappings", []))
        if mapping is None:
            raise PricingError(f"No reviewed meter mapping for {key(deployment)}")
        rates, evidence = {}, {}
        for name in RATES:
            selector = mapping["meters"][name]
            if name == "cachedInput" and selector == "same-as-input":
                # Explicitly reviewed lack of a cached discount, never an implicit zero.
                rates[name] = rates["input"]
                evidence[name] = "same-as-input"
            else:
                rates[name], evidence[name] = retail_rate(selector, deployment, items, as_of)
        evidence = {"mapping": mapping, "meters": evidence}
        source = "retail"
    if number(rates["input"]) == 0 or number(rates["output"]) == 0:
        raise PricingError(f"Input/output rates must be positive: {key(deployment)}")
    if number(rates["cachedInput"]) > number(rates["input"]):
        raise PricingError(f"Cached input exceeds input: {key(deployment)}")
    return {**deployment, "usdPerMillion": rates, "source": source,
            "sourceHash": digest(evidence), "evidence": deepcopy(evidence)}


def card_hash(entries):
    return digest(entries)


def generate(deployments, items, config, previous, effective):
    instant(effective)
    if previous and previous.get("entries"):
        validate_card(previous)
    if not deployments:
        raise PricingError("Cannot generate an empty price list")
    entries = {}
    for deployment in sorted(deployments, key=key):
        identifier = key(deployment)
        if identifier in entries:
            raise PricingError(f"Duplicate deployment: {identifier}")
        entries[identifier] = resolve(deployment, config, items, effective)
    source_hash = card_hash(entries)
    if previous and previous.get("sourceHash") == source_hash:
        validate_card(previous)
        return previous
    card = {"schemaVersion": 1, "version": f"{effective[:10]}-{source_hash[:16]}",
            "effectiveFrom": effective, "currency": "USD", "unit": "1M tokens",
            "sourceHash": source_hash, "entries": entries}
    named_value(card)  # Reject incompatible aliases / oversize cards before writing any artifact.
    return card


def validate_card(card):
    if card.get("schemaVersion") != 1 or card.get("currency") != "USD" or card.get("unit") != "1M tokens":
        raise PricingError("Unsupported rate-card schema, currency or unit")
    if not card.get("entries") or card.get("sourceHash") != card_hash(card["entries"]):
        raise PricingError("Empty rate card or source hash mismatch; generate with live discovery first")
    instant(card["effectiveFrom"])
    expected = f"{card['effectiveFrom'][:10]}-{card['sourceHash'][:16]}"
    if card["version"] != expected:
        raise PricingError("Rate-card version does not match its content")
    for identifier, entry in card["entries"].items():
        if key(entry) != identifier or entry["source"] not in ("retail", "override", "allocation"):
            raise PricingError(f"Invalid rate-card entry: {identifier}")
        if digest(entry["evidence"]) != entry["sourceHash"]:
            raise PricingError(f"Entry evidence hash mismatch: {identifier}")
        rates = entry["usdPerMillion"]
        if number(rates["input"]) <= 0 or number(rates["output"]) <= 0:
            raise PricingError(f"Missing positive price: {identifier}")
        if number(rates["cachedInput"]) > number(rates["input"]):
            raise PricingError(f"Invalid cached price: {identifier}")


def named_value(card):
    validate_card(card)
    aliases = {}
    for entry in card["entries"].values():
        alias = entry["deployment"].lower()
        rates = tuple(number(entry["usdPerMillion"][name]) for name in RATES)
        if alias in aliases and aliases[alias] != rates:
            raise PricingError(f"Conflicting rates for gateway deployment {alias}; "
                               "the current policy cannot distinguish region/version/SKU")
        aliases[alias] = rates
    # Third field is cached input; old policies still consume fields zero and one.
    value = ";".join(f"{alias}={text(rates[0])}/{text(rates[2])}/{text(rates[1])}"
                     for alias, rates in sorted(aliases.items()))
    value += f";__pricingVersion={card['version']}"
    if len(value.encode("utf-16-le")) // 2 > LIMIT:
        raise PricingError(f"model-pricing exceeds the {LIMIT}-character named-value limit")
    return value


def diff(previous, current):
    old = previous.get("entries", {}) if previous else {}
    new = current["entries"]
    lines = [f"# Model price diff: {current['version']}", "",
             "USD per 1M tokens; input / cached input / output.", "",
             "| Deployment / model / version / SKU / region | Before | After | Source |",
             "|---|---|---|---|"]
    for identifier in sorted(set(old) | set(new)):
        before, after = old.get(identifier), new.get(identifier)
        if before == after:
            continue
        def describe(entry):
            return "absent" if entry is None else " / ".join(
                entry["usdPerMillion"][name] for name in RATES)
        lines.append(f"| {identifier.replace('|', ' / ')} | {describe(before)} | "
                     f"{describe(after)} | {after['source'] if after else 'removed'} |")
    if old == new:
        lines.append("\nNo price, deployment or source changes.")
    return "\n".join(lines) + "\n"


def save_card(card, previous, directory):
    directory = Path(directory)
    if previous and previous.get("entries") and previous["version"] != card["version"]:
        target = directory / "history" / f"{previous['version']}.json"
        if target.exists() and load(target) != previous:
            raise PricingError(f"Refusing to overwrite rate-card history: {target}")
        write(target, previous)
    if previous != card:
        write(directory / "rate-card.json", card)
        (directory / "price-diff.md").write_text(diff(previous, card), encoding="utf-8")


def coverage(card, deployments):
    if set(card["entries"]) != {key(row) for row in deployments}:
        raise PricingError("Live deployments differ from the approved rate card; regenerate and review")


def arm_request(method, url, token, body=None, etag=None):
    if not url.startswith(ARM + "/"):
        raise PricingError("Unexpected ARM request host")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if etag:
        headers["If-Match"] = etag
    request = Request(url, method=method, headers=headers,
                      data=json.dumps(body).encode() if body is not None else None)
    with urlopen(request, timeout=90) as response:
        raw = response.read()
        result = json.loads(raw) if raw else {}
        if response.headers.get("ETag"):
            result["etag"] = response.headers["ETag"]
        return result


def get_named_value(url, token):
    result = arm_request("GET", url, token)
    properties = result["properties"]
    if properties.get("secret") or properties.get("keyVault"):
        raise PricingError("Refusing to read or overwrite a secret/Key Vault named value")
    if "value" not in properties:
        # Some ARM API responses omit even non-secret values. Keep this permission scoped to model-pricing.
        secret_url = url.replace("?api-version=", "/listValue?api-version=")
        properties["value"] = arm_request("POST", secret_url, token)["value"]
    return result


READBACK_ATTEMPTS = 12
READBACK_DELAY_SECONDS = 5


def publish(card, deployments, apim_id, directory, apply=False):
    value = named_value(card)
    coverage(card, deployments)
    if instant(card["effectiveFrom"]) > datetime.now(timezone.utc):
        raise PricingError("Cannot publish a future-effective rate card")
    apim_id = resource_id(apim_id, "Microsoft.ApiManagement/service")
    if not apply:
        print(f"Validated {len(card['entries'])} deployments; {len(value)} characters; "
              f"version {card['version']}. No Azure writes (use --apply).")
        return
    url = f"{ARM}{apim_id}/namedValues/model-pricing?api-version=2024-05-01"
    token = az("account", "get-access-token", "--resource", ARM)["accessToken"]
    before = get_named_value(url, token)
    properties = before["properties"]
    body = {name: properties[name] for name in ("displayName", "secret", "tags") if name in properties}
    body["value"] = value
    # Preserve the baseline and use an ETag to avoid overwriting concurrent changes.
    etag = before.get("etag")
    if not etag:
        raise PricingError("APIM did not return an ETag; refusing an unguarded update")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write(directory / "publish-before.json", before)
    arm_request("PUT", url, token, {"properties": body}, etag)
    # APIM applies named-value PUTs asynchronously (202), so poll briefly before declaring a mismatch.
    for attempt in range(READBACK_ATTEMPTS):
        after = get_named_value(url, token)
        if after["properties"]["value"] == value:
            break
        if attempt < READBACK_ATTEMPTS - 1:
            time.sleep(READBACK_DELAY_SECONDS)
    else:
        raise PricingError("APIM read-back does not match the approved card")
    write(directory / "publish-receipt.json", {"version": card["version"],
          "sourceHash": card["sourceHash"], "publishedAt": datetime.now(timezone.utc).isoformat(),
          "apim": apim_id, "verified": True})
    print(f"Published and read back {card['version']}; gateway propagation may take several minutes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check", "publish"))
    parser.add_argument("--accounts", help="JSON file containing full account ARM IDs")
    parser.add_argument("--deployments", help="Offline discovery fixture; never accepted for --apply")
    parser.add_argument("--retail", help="Offline retail fixture (array or API Items object)")
    parser.add_argument("--overrides", default=str(HERE / "overrides.json"))
    parser.add_argument("--directory", default=str(HERE))
    parser.add_argument("--effective-from")
    parser.add_argument("--apim-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load(args.overrides)
    directory = Path(args.directory)
    previous = load(directory / "rate-card.json") if (directory / "rate-card.json").exists() else None
    if args.command == "check":
        named_value(previous or {})
        if args.deployments:
            coverage(previous, load(args.deployments))
        print("Rate-card hashes, prices, aliases and named-value size are valid")
        return
    if args.apply and (args.command != "publish" or args.deployments):
        raise PricingError("--apply requires publish with live account discovery")
    if args.deployments:
        deployments = load(args.deployments)
    elif args.accounts:
        deployments = discover(load(args.accounts), config.get("exclude", []))
    else:
        raise PricingError("Provide --accounts for live discovery or --deployments for offline generation")
    if args.command == "publish":
        if not args.apim_id:
            raise PricingError("publish requires --apim-id")
        publish(previous or {}, deployments, args.apim_id, directory, args.apply)
        return
    effective = args.effective_from or datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = load(args.retail) if args.retail else fetch(deployments)
    if isinstance(items, dict):
        if items.get("NextPageLink"):
            raise PricingError("Offline retail fixture must contain the complete paginated snapshot")
        items = items["Items"]
    card = generate(deployments, items, config, previous, effective)
    save_card(card, previous, directory)
    print(diff(previous, card))


if __name__ == "__main__":
    try:
        main()
    except (PricingError, KeyError, TypeError, ValueError, OSError, URLError,
            subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"Pricing lifecycle failed: {detail}", file=sys.stderr)
        sys.exit(1)
