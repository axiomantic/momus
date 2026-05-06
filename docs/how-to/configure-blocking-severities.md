# Configure which severities block PRs

You want to control when Momus posts `REQUEST_CHANGES` versus an advisory `COMMENT`. The lever is [`review.blocking_severities`](../reference/config-schema.md#review-blocking-severities) in `.momus.yaml`.

The severity scale is fixed: `critical`, `high`, `medium`, `low`, `nit`. What you configure is the subset that blocks merge. Anything not on the list still posts as a finding; it just does not flip the verdict.

## Default

```yaml
review:
  blocking_severities: [critical, high, medium]
```

`low` and `nit` are advisory. This is the styleseat default and a reasonable starting point for most repos.

## Block only critical and high

For a repo with strong CI, where mediums tend to be caught by tests anyway:

```yaml
review:
  blocking_severities: [critical, high]
```

Mediums still post and are still visible. They just do not block.

## Block everything except nits

For a strict repo where any non-nit finding is reason enough to hold a PR:

```yaml
review:
  blocking_severities: [critical, high, medium, low]
```

## Advisory mode (block nothing)

```yaml
review:
  blocking_severities: []
```

Every finding posts as a `COMMENT`. The bot becomes a second-opinion reviewer rather than a gate. Useful when introducing Momus to a team that has not yet decided how much weight to give it.

## Pair with the first-review APPROVE policy

[`post.first_review_approve_policy`](../reference/config-schema.md#post-first-review-approve-policy) decides whether an empty findings list becomes `APPROVE` on a brand-new PR. The two settings interact: a tight blocking list with `first_review_approve_policy: never` means the bot will mostly post `COMMENT`; a loose list with `if_no_blocking` means it will frequently `APPROVE`.

## See also

- [Reference: `review.blocking_severities`](../reference/config-schema.md#review-blocking-severities)
- [Explanation: severity and blocking](../explanation/review-philosophy.md#severity-and-blocking)
- [How-to: tune cost vs thoroughness](./tune-cost-vs-thoroughness.md)
