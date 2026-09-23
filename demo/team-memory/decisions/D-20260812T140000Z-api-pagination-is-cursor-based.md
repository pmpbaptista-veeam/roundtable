---
id: D-20260812T140000Z-api-pagination-is-cursor-based
title: API pagination is cursor-based
topics: ["api"]
status: active
supersedes: null
proposed_by: ada
agreed_by: ["grace"]
approved_by_human: null
source_question: null
created: 2026-08-12T14:00:00Z
---

## Context

List endpoints previously mixed offset and cursor pagination.

## Options considered

Offset (simple, but unstable under concurrent writes) vs cursor (stable,
uniform `next` token).

## Decision

All list endpoints paginate with opaque cursors (`?cursor=`, `?limit=`,
response carries `next_cursor`). No new offset-based endpoints.

## Consequences

The upcoming report export list must be cursor-based too.
