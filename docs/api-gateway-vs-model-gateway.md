# API gateway vs model gateway

They are easy to mix up, so here is the split:

| | API gateway | Model gateway |
| --- | --- | --- |
| Sits between | clients and your services | your services and model providers |
| Here | `deploy/nginx.conf` + middleware in `sysdesign/app.py` | `ModelGateway` in `sysdesign/resilience.py` |
| Cares about | who is calling, request size, routing, coarse limits, request ids | which provider, timeouts, retries, breakers, fallback, token cost |
| Fails how | 401/403/404/413/429 to the client | switches provider, or returns a clear degraded answer |
| Knows tokens? | no (request counts only) | yes (it is where usage is measured) |

The admission controller sits between the two. It needs identity, which comes
from the API gateway, and token estimates, which only the model side
understands.
