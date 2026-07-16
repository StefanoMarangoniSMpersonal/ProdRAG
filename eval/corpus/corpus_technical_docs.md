## Platform Overview

NovaBridge is a cloud-based integration platform developed by Stratos Software Inc., headquartered in Vancouver, Canada. It allows developers to connect disparate enterprise systems (ERP, CRM, HRIS) through a unified API gateway. The platform launched in public beta in June 2021 and reached general availability (GA) in January 2022. As of 2024, NovaBridge serves over 2,300 enterprise customers and processes an average of 1.4 billion API calls per day.

The platform is available in three tiers: Starter (free, up to 50,000 API calls/month), Professional ($490/month, up to 5 million calls/month), and Enterprise (custom pricing, unlimited calls with a dedicated SLA).

---

## Authentication

### API Keys

All requests to the NovaBridge API require a valid API key passed in the `X-NB-Key` header. API keys are scoped to one of three permission levels: `read`, `write`, or `admin`. Keys can be generated from the NovaBridge Dashboard under Settings → API Keys. Each organization can have a maximum of 50 active API keys.

API keys do not expire by default. However, the Enterprise tier supports automatic key rotation with configurable intervals of 30, 60, or 90 days. When rotation occurs, the previous key remains valid for a 48-hour grace period before being revoked.

### OAuth 2.0

For user-facing integrations, NovaBridge supports OAuth 2.0 with the authorization code flow. The authorization endpoint is `https://auth.novabridge.io/authorize` and the token endpoint is `https://auth.novabridge.io/token`. Access tokens expire after 60 minutes. Refresh tokens are valid for 30 days and can be used only once — each refresh issues a new refresh token (rotation model).

---

## Core API Reference

### Base URL

All API endpoints are available at `https://api.novabridge.io/v3/`. The current stable version is v3, released in March 2024. Version v2 is in maintenance mode and will be sunset on December 31, 2025. Version v1 was fully retired on June 30, 2023.

### Rate Limits

| Tier         | Requests/sec | Burst limit | Monthly cap        |
|--------------|-------------|-------------|---------------------|
| Starter      | 10          | 25          | 50,000              |
| Professional | 100         | 250         | 5,000,000           |
| Enterprise   | 500         | 1,000       | Unlimited           |

Rate-limited responses return HTTP 429 with a `Retry-After` header indicating seconds to wait. The platform uses a sliding window algorithm with a 1-minute granularity.

### Connectors

NovaBridge offers 148 pre-built connectors as of 2024. The most popular connectors by usage are:

1. Salesforce CRM — used by 68% of customers
2. SAP ERP — used by 41% of customers
3. Microsoft Dynamics 365 — used by 37% of customers
4. Workday HRIS — used by 29% of customers
5. Shopify — used by 24% of customers

Custom connectors can be built using the Connector SDK (available in Python, Java, and Node.js). Custom connectors must pass a validation suite of 23 automated tests before they can be deployed to production.

---

## Data Transformation Engine

### Overview

NovaBridge includes a built-in data transformation engine called "Forge." Forge allows developers to write transformation rules in a domain-specific language (DSL) called ForgeScript. ForgeScript is a declarative language with JSON-like syntax that supports field mapping, type coercion, conditional logic, and aggregation.

### ForgeScript Example

```
transform OrderSync {
  input: salesforce.Order
  output: sap.SalesDocument

  map {
    sap.DocNumber   <- salesforce.OrderNumber
    sap.CustomerID  <- salesforce.AccountId
    sap.TotalAmount <- round(salesforce.TotalPrice, 2)
    sap.Currency    <- upper(salesforce.CurrencyIsoCode)
    sap.Priority    <- if salesforce.TotalPrice > 10000 then "HIGH" else "NORMAL"
  }
}
```

### Performance

Forge processes transformations at an average throughput of 12,000 records per second on the Professional tier and 45,000 records per second on the Enterprise tier. Batch transformations of up to 10 million records can be queued via the `/v3/forge/batch` endpoint; batch jobs are processed asynchronously and results are delivered via webhook.

---

## Error Handling

NovaBridge uses standard HTTP status codes. All error responses include a JSON body with the following structure:

```json
{
  "error": {
    "code": "CONNECTOR_TIMEOUT",
    "message": "The Salesforce connector did not respond within 30 seconds.",
    "request_id": "nb-req-8a3f2c",
    "timestamp": "2024-03-15T14:22:01Z"
  }
}
```

Common error codes include:

- `AUTH_INVALID_KEY` (HTTP 401) — The API key is missing, malformed, or revoked.
- `AUTH_INSUFFICIENT_SCOPE` (HTTP 403) — The API key lacks the required permission level.
- `RATE_LIMIT_EXCEEDED` (HTTP 429) — Too many requests; see `Retry-After` header.
- `CONNECTOR_TIMEOUT` (HTTP 504) — The downstream system did not respond within the timeout window (default 30 seconds, configurable up to 120 seconds).
- `TRANSFORM_INVALID_SCRIPT` (HTTP 422) — The ForgeScript definition contains syntax errors.
- `PAYLOAD_TOO_LARGE` (HTTP 413) — Request body exceeds the 5 MB limit.

---

## Webhooks

NovaBridge supports outbound webhooks for event-driven architectures. Webhook endpoints must use HTTPS and respond with a 2xx status code within 10 seconds. If the endpoint fails, NovaBridge retries with exponential backoff: 1 minute, 5 minutes, 30 minutes, 2 hours, and 12 hours. After 5 consecutive failures, the webhook is automatically disabled and the organization admin receives an email notification.

Webhook payloads are signed with HMAC-SHA256 using a secret key unique to each webhook registration. The signature is sent in the `X-NB-Signature` header.

---

## Versioning and Changelog

### Version 3.2 (October 2024)

- Added support for GraphQL queries on 12 connectors (Salesforce, Shopify, GitHub, and 9 others).
- Introduced "Forge Snapshots" — immutable, versioned copies of transformation rules for audit compliance.
- Reduced average API latency by 18% through connection pooling improvements.

### Version 3.1 (June 2024)

- Launched the Connector SDK for Java (previously only Python and Node.js).
- Increased batch transformation limit from 5 million to 10 million records.
- Fixed a bug where OAuth refresh tokens were not properly rotated under high concurrency (CVE-NB-2024-0042).

### Version 3.0 (March 2024)

- Complete rewrite of the authentication layer to support per-key permission scoping.
- Introduced ForgeScript DSL, replacing the legacy XML-based mapping format.
- Deprecated v2 endpoints (sunset December 31, 2025).

---

## Known Limitations

- ForgeScript does not currently support recursive data structures or nested array transformations deeper than 3 levels.
- The Starter tier does not support webhooks.
- Custom connectors built with the Python SDK require Python 3.9 or higher; Python 3.8 support was dropped in version 3.1.
- Real-time streaming (via WebSockets) is available only on the Enterprise tier and is limited to 10 concurrent streams per organization.
