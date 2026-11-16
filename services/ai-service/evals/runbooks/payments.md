# Payments

## Gateway outage

When the payment gateway answers 503 or times out, the provider is having an outage. Retry only a few times, wait
longer between attempts (exponential backoff with jitter), and never retry immediately in a tight loop: a storm of
instant retries overloads a struggling gateway and makes the outage last longer. Show the customer a clear
"try again later" message and keep orders unpaid.

## Rotated or expired credentials

When the gateway rejects every request with 401 "invalid api key", the provider rotated the key or ours expired.
This is a configuration problem, not a code bug. Put the new key in the GATEWAY_API_KEY environment variable and
redeploy. Never log Authorization headers: they contain the live key.

## Duplicate charges

Protect large orders against double charging with an idempotency key stored on the server. Build the store with its
constructor so its internal map is initialised; writing to a nil map panics.
