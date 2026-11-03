# Photo Triage Agent — Windows / PhotoTools fork

## RESTORE_FAMILY integration

PhotoTools' `photos.py restore-family` restores photos and videos dropped into
`processed_files/RESTORE_FAMILY` and enrolls their visible faces locally. Matching
photos are protected from junk/TikTok nominations; **NSFW and quality checks are
independent, and exact duplicates still clean up automatically**. This does not
name people or add family likenesses to the NSFW safe list. See the deployed
PhotoTools README for original-location restoration and reports.
Every processing run handles RESTORE_FAMILY first, before imports, other inboxes,
or gallery checks. Unreadable input blocks scan startup; cloud-only destinations remain pending while other media proceeds. Help/status stay read-only.

The portable `photo_triage.faces` module uses pinned [OpenCV YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
and [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)
models on CPU. Install with `python scripts/install_family_models.py --output C:/PhotoTools/models/family-faces`
after installing this package. Downloads are SHA-256 verified; model licenses
are saved alongside them. Only local vectors and receipts persist, never image
crops or uploaded photos. All usable faces in an input are enrolled, so use
photos containing only the people you want protected. Recognition is approximate;
several views improve coverage. No-face inputs restore normally without an
enrollment failure. Videos use up to 12 evenly spaced frames, decoded only in memory; a matching face protects the video from junk/TikTok while NSFW and quality still run. Sampling can miss brief faces.

Ordinary photos labeled with `personal_record` evidence remain non-junk and do
not require uncertainty review. The original model evidence remains in the
report. Conflicting junk evidence and explicitly uncertain results remain gaps.

This fork adds a portable local analysis package and resumable batch integration

for PhotoTools. It shares decoded-image checks without taking over gallery

moves, deletion decisions, inboxes, or cloud delivery.

## Install the portable core

```powershell

python -m pip install .

python -m unittest discover -s tests -v

```

The core works with a persistent local llama.cpp vision server. Images remain in

memory and are sent only to loopback; inference never uploads to a remote API.

```python

from photo_triage.vision import VisionProvider, junk_result

from photo_triage.batch import batches

provider = VisionProvider(

    "http://127.0.0.1:18765/v1/chat/completions",

    model="Qwen3-VL-4B-Instruct-Q8_0",

)

# Each image is a decoded PIL image; keep the server running across batches.

for batch in batches(images, limit=100):

    for image in batch:

        print(junk_result(provider.inspect(image), list(image.size)))

```

PhotoTools uses its existing **Qwen3-VL 4B** model through this fork. The upstream

LFM model ran locally but missed the joke-sign test cases; Qwen passed all ten

known missed-meme examples. LFM is still available through the default provider

on port 18766 and the explicit checksum-pinned `scripts/install_windows_model.py`

installer. The installer assumes the existing Windows PhotoTools llama.cpp

runtime; it does not start cleanup.

## Use resumable batches in PhotoTools

```powershell

python photos.py triage-batch --selection selection.json --output C:/PhotoTools/runs/triage-test --batch-size 100 --limit 500

```

Repeat with the same output directory to process the next new items. Successful

results resume only after current content and rule/model verification. The limit

counts new attempts; cached results do not consume it. This command is read-only

and does not process review inboxes. Normal gallery and import inference also

use bounded batches, while retaining duplicate-first and asset-group sequencing.

See [the integration guide](docs/PHOTOTOOLS.md) for the full capability map,

installation boundaries, model comparison, resume semantics, and validation

limits. The companion adapter source is in `integrations/phototools/`.

Run `python scripts/check_local_controls.py` only when the configured Qwen server

is running to exercise four in-memory non-junk controls. This accesses no gallery.

Core CI runs on Windows and Linux without downloading a model or retaining media.

## Original macOS application

The original web app and its trash workflow are separate from the PhotoTools
integration. See the [upstream documentation](https://github.com/chintan-projects/photo-triage-agent).
To use a persistent server in that app, install this root package in its backend
environment and set `PHOTO_TRIAGE_ENDPOINT` and `PHOTO_TRIAGE_MODEL`.
PhotoTools does not deploy or invoke its trash API.

PhotoTools review routing accepts validated core category evidence for photos and sampled videos. Supported meme, screenshot, and receipt findings reach FOR_REVIEW without legacy detector labels. Incomplete or unsupported findings remain explicit gaps; this does not authorize permanent deletion.


Pending family restores use a separate status and never enter the NSFW safe index.

Restore recorded cloud-only paths directly from local inbox bytes: verify a temporary local copy, replace the placeholder without downloading it, verify the destination, then clear the inbox. Resident content conflicts remain protected.

PhotoTools permits 15 minutes per video analysis/enrollment, including decoder and queue waits. Photos and file copying retain their existing limits. Restart existing workers to apply changed execution deadlines.

Run-scoped accepted-exceptions.json entries with skip_for_current_run exclude only the explicitly approved paths from fresh inventory. Restart carries this file forward; no global exclusion or deletion is created.
# Local classification recovery

Ambiguous junk classifications receive one additional full-scene check at up to
1920 pixels, with guidance distinguishing photographed screens and reference
photos from screenshots. Both observations are retained. Clear results use one
inference. The response budget is 512 tokens with concise observations, and
malformed or truncated model output gets one formatting retry within the same
request deadline, with a larger token budget. Persistent errors remain explicit.
A second uncertain result is never
converted into an unsupported junk nomination. Images stay in request memory.

