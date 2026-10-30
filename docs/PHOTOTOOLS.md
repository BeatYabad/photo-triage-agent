# PhotoTools integration

The portable `photo_triage` package is the shared analysis implementation. The
`integrations/phototools` directory contains the corresponding adapter/overlay
source for the existing PhotoTools project; it is not a standalone gallery app.
It needs the rest of that project's established import, decoder, inbox, metadata,
NSFW, and provider modules. No account settings, ledgers, gallery paths, photos,
or model weights are published here.

## Ownership of checks

| Capability | Implementation after integration |
| --- | --- |
| Memes, screenshots, receipts | This fork's `VisionProvider`, concrete visual observations and category-specific evidence |
| Personal/reference photos and records | Classified distinctly; missing EXIF and generic text never prove junk |
| Blur/detail/exposure | This fork's decoded-image metrics; existing exposure-only nomination policy preserved |
| Visual similarity beyond byte hashes | This fork's 256-bit pHash index and candidate-only report, separate per gallery |
| Explicit Czkawka comparisons | Pinned read-only Czkawka adapter now maintained in this fork |
| Exact duplicate retirement | Existing fresh SHA-256/length verification and gallery boundaries remain authoritative |
| NSFW, TikTok, video sampling, RAW/HEIC/MPO decoding | Existing PhotoTools capabilities retained; sampled decoded video frames use this fork for junk and quality |
| RESTORE/SFW/DELETE/CONFIRMED, import, cloud delivery, asset bundles | Existing PhotoTools coordinators retain ownership |

The original web app's filename/EXIF screenshot heuristics are not used as
PhotoTools junk evidence. Its perceptual groups and trash API are not deletion
authority. Normal photo analysis produces `visual-candidates.json` without a
second gallery walk. The report records unavailable features separately and caps
pair output at 10,000 with `truncated=true` when more results require inspection.
Visual matches cannot remove differently encoded originals automatically.

## Models

The upstream LFM2.5-VL 1.6B model runs on Windows with the installed llama.cpp
runtime. However, its evidence-constrained test missed joke-sign images and
misclassified their content. PhotoTools therefore uses its already installed
Qwen3-VL 4B model through this fork's interchangeable loopback provider. This
also shares the existing server with the retained NSFW visual review, rather
than loading one model per worker. LFM remains an optional, separately installed
provider; it is not the active PhotoTools classifier.

Model scores are not calibrated deletion probabilities. Contradictory evidence,
unreadable inputs, inference failures, and unsupported resources remain explicit
gaps. This integration does not introduce person identification or face tagging.

## Batches and resume

`triage_batch_size` defaults to 500. Gallery processing completes its fresh
inventory and exact-duplicate phase before handing unique content groups to
bounded batches. Import keeps its complete association/destination preparation
and uses the same bounded inference windows. A single persistent model serves
the windows; aliases retain independent gallery and asset metadata.

The read-only `photos.py triage-batch` task accepts an explicit selection JSON
array of `{ "path": "...", "library": "icloud" }` objects. It runs no inbox,
move, deletion, upload, or cleanup actions. Its `--limit` bounds new attempts,
not already verified cache hits. Repeating the command with the same output
directory freshly checks current bytes and resumes successful exact-content
results; changed bytes/rules/models invalidate them. Failures and uncertainty
are retried rather than cached as successful. A five-minute worker limit bounds
each file. Cache databases contain results, never image bytes.

```
python photos.py triage-batch --selection selection.json --output C:/PhotoTools/runs/triage-test --batch-size 100 --limit 500
```

Repeat the same command for the next set of new files. Inspect `report.json` and
`visual-candidates.json`. The normal cleanup queue remains separate and stopped
until explicitly requested. Classification itself sends only in-memory image
data to loopback. The explicit Czkawka HEIC comparison path may create approved
temporary decoded comparison images outside the project and deletes them when
finished. No retained thumbnails, frames, or automatic media backups are added.

## Validation and limits

Windows tests exercise the core, existing analyzers, PhotoTools adapters, batch
scheduling, exact identity and gallery boundaries, inbox decisions, import,
video sampling, and MPO decoding. Actual inference testing used 10 previously
missed images and four in-memory non-junk controls. Qwen nominated all 10 as
memes, left all four controls unflagged, and the visual index found 10 candidate
pairs across the three repeated-picture families. A 4 + 4 + 2 new-item sequence
verified resume, with eight verified cache hits on the final invocation.

This is a small regression sample, not a general accuracy benchmark. Actual
wanted family-photo false-positive coverage is not established by synthetic
controls. The original macOS web UI and its trash actions have not been deployed
or certified as a replacement for PhotoTools gallery actions.
