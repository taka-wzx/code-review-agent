# Administration Console

The service exposes a small same-origin console at `/admin`. It is intended for
organization administrators and maintainers who already have a service bearer
token. The console has no build step and does not load third-party runtime
assets.

## Authentication

The login shell is intentionally not a data endpoint. After the operator enters
a bearer token, the browser calls `/v1/principal` and renders only the views
allowed by the returned role. The token is held in `sessionStorage` for the
current browser tab and removed by Disconnect or when authentication fails. It
is never written into HTML, JavaScript, cookies, URL parameters, or
`localStorage`.

The browser still sends every API request with the bearer token. The UI's role
gating is only a usability boundary; `service.py`, `service_core.py`, and the
database authorization checks remain the security boundary. Cross-organization
requests and unauthorized writes are rejected by the API and audited there.

## Views

- Maintainers and organization administrators can inspect pending publication
  proposals. Approve and Reject both require an explicit browser confirmation
  and submit the proposal's current payload hash and nonce.
- Organization administrators can list and update registered repositories,
  manage organization policy, manage memberships, and read audit events.
- Viewers and reviewers receive the read-only shell without organization
  administration or publication controls.

## Deployment Notes

Serve the console behind the same origin as the API. Keep the existing exact
origin and host allowlists. Do not put service tokens into reverse-proxy query
parameters or static files. A production deployment should use HTTPS and the
same authentication backend as the API.
