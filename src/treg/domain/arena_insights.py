"""Conservative classification for database-backed Arena observations. No I/O."""
import json
import re
from urllib.parse import parse_qs, urlsplit, unquote

NO_RESULT = re.compile(r'companynotfound|profile_not_found|(?:company|person|profile) (?:could not be |not )found|no (?:matching profile|linkedin profile|records|companies) (?:was |were )?found|does(?: not|n.t) exist in our database|does not resolve to a known person', re.I)
FUNDS = re.compile(r'insufficient[_ ](?:balance|credits|funds)|credits_exhausted|credits? (?:have been |is |are )?exhausted|used all of its credits|not enough credits|out of credits|credits? remaining.*(?:zero|\b0\b)', re.I)
RATE = re.compile(r'rate[_ -]?limit|too many requests|too_many_requests', re.I)
INVALID = re.compile(r'wrong_params|params_invalid|parameters misconfigured|validation error|invalid[_ ](?:parameter|params|email|url|name)|field is required|first name is required|missing required', re.I)

def parse_request(text):
    text = text or ''
    query, body = {}, {}
    if text.startswith('?'):
        q, sep, rest = text[1:].partition(' {')
        query = {k: v[-1] for k, v in parse_qs(q, keep_blank_values=True).items()}
        if sep:
            try: body = json.loads('{' + rest)
            except ValueError: pass
    elif text.lstrip().startswith(('{', '[')):
        try: body = json.loads(text)
        except ValueError: pass
    return query, body

def invalid_request(query, body):
    # Values actually sent to a handle field must not be a URL, path, or @handle.
    handle = query.get('linkedin_handle')
    if isinstance(handle, str) and handle and (any(s in unquote(handle) for s in ('/', ':', '@'))):
        return 'invalid_linkedin_handle'
    return None

def response_doc(text):
    text = re.sub(r'^\[[^\]]*\]\s*', '', text or '')
    try: return json.loads(text)
    except ValueError: return None

def body_failure(doc):
    if not isinstance(doc, dict): return None
    # Restrict inspection to explicit error/status fields; arbitrary profile prose is not evidence.
    selected = {k: doc[k] for k in ('error', 'errors', 'error_details', 'code', 'status', 'message', 'detail') if k in doc}
    s = json.dumps(selected)
    if FUNDS.search(s): return 'excluded_upstream_balance'
    if RATE.search(s): return 'excluded_rate_limit'
    if INVALID.search(s): return 'excluded_invalid_request'
    if doc.get('error') or doc.get('errors') or doc.get('success') is False or str(doc.get('status', '')).lower() in ('error', 'failed', 'failure'):
        return 'unresolved_body_error'
    return None

def error_outcome(row, policy, request=None):
    if row.get('refused_by'):
        return 'excluded_treg_' + row['refused_by'], 'explicit_refused_by'
    query, body = request if request is not None else parse_request(row.get('error_request'))
    invalid = invalid_request(query, body)
    if invalid: return 'excluded_invalid_request', invalid
    status = row['status_code']
    # Evidence prefixes contain rate-limit headers on healthy responses too. A header named
    # x-ratelimit-limit is not a throttling error; inspect the response body, not that prefix.
    text = re.sub(r'^\[[^\]]*\]\s*', '', row.get('error_response') or '')
    if FUNDS.search(text) or status == 402:
        return 'excluded_upstream_balance', 'status_or_explicit_credit_error'
    if RATE.search(text) or status == 429:
        return 'excluded_rate_limit', 'status_or_explicit_rate_error'
    if status in (401, 403):
        return 'excluded_access', 'http_access_failure'
    if status in (408, 425) or status >= 500:
        return 'excluded_service_error', 'http_service_failure'
    if INVALID.search(text) or status in (400, 405, 406, 409, 415, 422, 431):
        return 'excluded_invalid_request', 'http_or_explicit_request_error'
    if status == 451:
        return 'excluded_policy_restriction', 'http_451'
    if status == 404:
        if NO_RESULT.search(text):
            return 'miss', 'explicit_not_found_response'
        bare = re.sub(r'^\[[^\]]*\]\s*', '', text).strip().lower()
        path = urlsplit(row.get('path') or '').path.rstrip('/')
        expected = (policy.get('path') or '').rstrip('/')
        if row['endpoint_id'].startswith('aviato.') and bare == 'not found' and row.get('method') == policy.get('method') and path.endswith(expected) and (query or body):
            return 'miss', 'documented_aviato_entity_404_on_correct_route'
        return 'unresolved_404', 'insufficient_evidence_for_entity_miss'
    if not 200 <= status < 300:
        return 'unresolved_status', 'unclassified_http_status'
    return None, None


RULES_VERSION = "1"


def input_label(endpoint, adapter, query, body, path=""):
    from .catalog.routing import paths as P
    if isinstance(body, list):
        body = body[0] if body else {}
    request = {"queryParams": query, "body": body if isinstance(body, dict) else {}}
    values = {}
    template = endpoint.get("path") or ""
    parameters = re.findall(r"\{([^}]+)\}", template)
    pattern = re.escape(template)
    for parameter in parameters:
        pattern = pattern.replace(re.escape("{" + parameter + "}"), "([^/]+)")
    match = re.search(pattern + "/?$", urlsplit(path).path) if parameters else None
    path_values = dict(zip(parameters, match.groups())) if match else {}
    for field, target in adapter.in_map.items():
        value = (query.get(target.split(".", 1)[1]) or path_values.get(target.split(".", 1)[1])) if target.startswith("pathParams.") else P.get_path(request, target)
        if value not in (None, "", []):
            values[field] = value
    if endpoint["provider"] == "lusha" and isinstance(body, dict) and body.get("contacts"):
        contact = body["contacts"][0]
        if isinstance(contact, dict):
            for native, canonical in [("linkedinUrl", "linkedin_url"), ("email", "email"), ("firstName", "first_name"), ("lastName", "last_name"), ("companyDomain", "domain")]:
                if contact.get(native):
                    values[canonical] = contact[native]
    linkedin = bool(values.get("linkedin_url") or values.get("linkedin_handle"))
    if endpoint["capability"] == "companies.enrich":
        flags = [(values.get("domain"), "domain"), (values.get("name"), "name"), (linkedin, "linkedin_url")]
    else:
        flags = [(values.get("email"), "email"), (linkedin, "linkedin_url"),
                 (values.get("domain") and (values.get("full_name") or values.get("first_name") and values.get("last_name")), "name_domain")]
    labels = [label for value, label in flags if value]
    return labels[0] if len(labels) == 1 else "unknown"


def classify_record(row, endpoint, adapter, contract, evidence=None):
    from . import arena
    # Explicitly exclude credentials/overflow/cache: these are not comparable platform observations.
    if row["kind"] != "call" or row["cached"] or row["credential_tier"] not in ("platform", None):
        return "unknown", "excluded_scope"
    query, body = parse_request(row.get("error_request"))
    if evidence:
        query, body = evidence[0], evidence[1]
    label = input_label(endpoint, adapter, query, body, row.get("path") or "")
    category, _ = error_outcome(row, endpoint, (query, body))
    if category:
        return label, category
    if row["status_code"] in (202, 222):
        return label, "excluded_pending"
    if row["credential_tier"] != "platform":
        return label, "excluded_scope"
    if not evidence or evidence[2] is None:
        return label, "unresolved_archive"
    if not row.get("params_hash"):
        return label, "unresolved_identity"
    problem = body_failure(evidence[2])
    if problem:
        return label, problem
    verdict, _ = arena.classify(contract, adapter, endpoint, row["status_code"], evidence[2])
    return label, verdict if verdict in ("hit", "miss") else "unresolved_body"
