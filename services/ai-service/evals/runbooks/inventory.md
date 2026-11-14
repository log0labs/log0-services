# Inventory

## Out of stock

When a reservation asks for more units than are available, answer 409 with the SKU and let the storefront refuse
the checkout cleanly. The storefront must await the reservation and handle that answer.

## Unstocked products

A product can exist in the catalog before it has an inventory record. Looking such a SKU up returns nothing; treat
it as out of stock and never unwrap the lookup, or the request task panics and the caller only sees a dropped
connection.

## Slow reservations

Reservations that suddenly take seconds usually mean a lock is held for too long. Do not hold the stock lock while
writing the audit trail or doing any other slow work: take the lock, change the stock, release it, then write the
audit entry. A burst of checkouts then queues behind the lock and the storefront times out.
