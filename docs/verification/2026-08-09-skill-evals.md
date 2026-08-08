# Skill trigger evaluation

## Candidate

- Skill: `social-media-favorites-archiver`
- Description length: 124 characters
- Candidate description: the exact implementation-plan discovery description
- Eval set: 20 synthetic, private-data-free requests in `evals/evals.json`

The set contains ten should-trigger cases spanning Chinese and English requests, Bilibili/B站, Xiaohongshu/小红书/RedNote, Douyin/抖音, multi-platform migration, scheduling, and recovery. Ten near-miss negatives cover single-video transcription, one-image OCR, ordinary video summarization, public-account scraping, marketing copy, reposting, commenting, unrelated browser bookmarks, creator mirroring, and manual note templates.

## Method

Three independent classifier passes received only the Skill name, description, and synthetic request text. Each pass returned one boolean decision per case. The classifier was instructed to decide whether the specialized personal favorites collection archive/sync/migration workflow should load and not to execute the requests. No platform session, account data, repository secret, live content, or external write was involved.

## Results

| Pass | Correct | False positives | False negatives | Score |
|---:|---:|---:|---:|---:|
| 1 | 20/20 | 0 | 0 | 100% |
| 2 | 20/20 | 0 | 0 | 100% |
| 3 | 20/20 | 0 | 0 | 100% |

Aggregate: 60/60 decisions correct, with zero observed variance.

## Decision

Retain the original 124-character description. The test evidence provides no false-positive or false-negative signal that would justify changing it. Bundle tests separately enforce the exact name/description, optional environment declarations, reference integrity, MIT-0 boundary, size, install command, and absence of secret/private-path patterns.
