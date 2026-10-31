October 27 correction: a still alone is acceptable; imports preserve supplied
motion as screened video too. Missing or ambiguous still pairing is not a review
nomination or deletion permission. Normal filters and exact duplicate checks
still apply. Separate local resources do not prove native Live Photo assembly.

# Photo tools: how to run each task

## Restore and protect family photos

Drop family photos or videos in `C:/Users/steve/OneDrive/processed_files/RESTORE_FAMILY`.
Every normal processing run handles this inbox first, before imports, other
decision inboxes, or gallery checks, and checks it again at completion. Help and
status remain read-only. Unreadable family inputs prevent scan startup; cloud-only destinations stay pending while other files proceed.
To process
it immediately, run `C:/PhotoTools/runtime/Scripts/python.exe C:/PhotoTools/photos/photos.py restore-family`.
This restores recorded original locations, or dated Personal Camera Roll when
origins are unknown, verifies exact restored bytes, and clears successful inputs.
It learns every usable face in each submitted photo, without naming anyone.
Use photos containing only people you want protected; several clear views help.

Matching photos skip junk and TikTok filtering. **NSFW and quality checks remain
independent and still run. Exact duplicates are still removed automatically.**
Family restoration adds no NSFW safe-list entry. Existing separate RESTORE/SFW
decisions retain their original meaning. Later explicit DELETE decisions still apply.
Face matching is approximate; missed faces remain possible. No detected face
still restores and protects that exact reference from junk; it is not an enrollment failure.
Videos enroll and compare faces from up to 12 evenly spaced frames in memory. A match protects the whole video from junk/TikTok only; NSFW and quality checks still run. Sampling can miss brief faces.

References are anonymous face vectors in `C:/PhotoTools/runs/review/family-faces.json`;
no face crops, copied photos, names, or cloud uploads are retained by the matcher.
Models live in `C:/PhotoTools/models/family-faces`. Enrollment has a five-minute
deadline. Errors retain the input for retry. New reference revisions invalidate
cached filtering results, and review moves recheck family protection.

Live Photos and standalone stills are distinct assets, even when their image
bytes match. They are reported separately, not counted as duplicate extras or
duplicate holds. Exact standalone copies still deduplicate against another
standalone copy. Live Photo pairing metadata does not authorize deleting a still.

Exact local duplicate cleanup does not require iCloud asset mappings or cloud
verification. It freshly checks SHA-256 and byte length of the extra and a
distinct retained same-gallery copy. Companion and sidecar checks still apply.

## Review selected files in resumable batches

`photos.py triage-batch --selection FILES.json --output C:/PhotoTools/runs/triage-test --batch-size 100 --limit 500`

Repeat with the same output directory for the next 500 new attempts. FILES.json
lists explicit `path` and `library` values. Current bytes are checked before
successful cached results resume. Reports show classifications, holds, and visual
matches beyond byte hashes. No cleanup, inbox actions, or media moves run here.

Normal filters now use the maintained photo-triage fork for junk, screenshot,
receipt, quality metrics and perceptual candidates. Its Qwen provider runs locally
and shares the installed server. NSFW, TikTok, video sampling, exact-duplicate
verification, review decisions and gallery delivery remain supported. Normal
gallery batches default to 500 unique content groups after exact duplicates.


## Compare selected pictures without cleanup

`photos.py compare-selection --selection FILES.json --output NEW-REPORT.json --allow-temporary-images`

FILES.json is an array of objects with `path` and `library`. This read-only command
compares only those pictures using Czkawka, including HEIC through temporary decoded
images outside this project. Temporary images are removed after comparison. The
report separates exact bytes, identical decoded pixels and perceptual candidates.
It never moves/deletes originals, processes inboxes, or restarts the cleanup queue.
Video/multi-frame comparison and replacement of the normal cleanup comparison
engine remain pending; existing cleanup commands have not been switched over.

MPO photos with a stale primary-image size now use a fully parsed JPEG boundary.
Every indexed image must still validate and decode; unknown bytes and incomplete
resources remain failures. The October 29 decoder deployment is recorded in
`../../outputs/mpo-decoder-deployment-20261029.json`. Active workers that loaded
the old decoder are covered by `repair_scan_failures.py`; its `failure-repair`
receipts report corrected results separately from the original coverage log.

Destination comparisons persist successful hashes in `hash-index.sqlite` beside
`review-state.json` (currently `C:/PhotoTools/runs/review/hash-index.sqlite`). Each
run still inventories current files. Unchanged file identities, size, write time,
Windows change time and provider attributes can reuse hashes; edits, replacements,
missing files or unavailable identity information trigger fresh reads or holds.
The first indexed comparison must read the bytes. Failed reads are never cached.
Corrupt or unavailable indexes fall back to fresh hashing and report the cache error.
`comparison-progress.json` reports candidates processed, cached/fresh hashes and
remaining files during comparison. Cached hashes are lookup hints: source retirement
still freshly verifies actual keeper/destination SHA-256 and byte length. No media
copies are stored in the index. `hash_index` may specify another metadata-only path.

Imports can adjust screening workers while running, finishing each file before
retiring a worker. To set a live ceiling, write `{"workers":2}` to the import run's
`scan/worker-control.json` (1–4); remove it for automatic memory-based resizing.
Check `scan/worker-status.json` for the active count and any memory hold.
See PERFORMANCE.md for limits and verification.

Pending October 27 deployment: imports inventory associations and compare current
gallery bytes first, then deliver complete screened groups while screening the
remaining files. One bounded delivery writer verifies destinations and records
receipts before removing sources. Companions wait together; failed checks remain
held. The final pass reconciles completed receipts. Deployment must wait until
the active archive import and its helpers exit, then pass the deployment manifest,
regression tests, and production GPU/runtime verification before the next import.

The shared cleanup/import filters use the installed GPU OCR and CLIP vision profile.
CLIP uses tested DirectML basic optimization with a one-time CPU fallback on errors.
Existing workers keep their loaded model until restart; new workers use this profile.
Normal commands stay the same; OCR failures retry on CPU and report the fallback.
For a read-only comparison, run `python benchmark_gpu_ocr.py --backend directml
--video VIDEO_PATH --output REPORT_PATH`, then repeat with `--backend cpu`.
Benchmark frames stay in memory. See ADVANCED.md for setup and measured limits.

Duplicate cleanup now checks current iCloud resource mappings when filenames
suggest companions. Separate original assets no longer stop at a filename clue.
Actual bundles, edits and unavailable/ambiguous mapping evidence remain held.
Relationships and exact keeper bytes are checked again before recycling.

Queued scan groups now consume completed duplicate-recycle receipts before
decoding, freshly verify the same-gallery keeper, and scan that keeper. If a
duplicate is recycled during scheduling, the scan switches once to its verified
keeper. Missing files without matching receipts remain failures.
For a run already using older loaded code, `python repair_scan_failures.py --run
RUN_FOLDER --owner-pid SCAN_PID` repairs its failed groups and follows new failures
until that scan exits. It preserves the original coverage log and records corrected
results under `failure-repair`; status reports those corrections separately.

To resolve filename holds from the current run without repeating classification:

```powershell
& $python resolve_duplicate_holds.py --config photos.local.json --plan 'CURRENT-RUN\streaming\duplicates\plan.json' --output (New-PhotoRun duplicate-holds) --apply
```

Explicit DELETE selections of separate OneDrive motion exports are recycled
after exact SHA-256/length and actual video-format verification. This approval
does not require a surviving still and never selects a still for deletion.
Native Apple resources and unrelated companion holds remain protected.

To process already-approved DELETE motion files, run:

```powershell
& $python delete_motion_plan.py --config photos.local.json --apply
```

This processes DELETE only, freshly checking both galleries for exact approved
content. Unavailable bytes and native asset holds stay in `review-delete-report.json`.

October 26: process RESTORE/SFW, DELETE and NSFW decision inboxes before the
gallery scan and again at the end. DELETE verifies current matching gallery
bytes during the initial inbox stage; it is not deferred until filtering ends.
Supported review findings are delivered continuously as their verified groups
finish analysis. Provider failures and ambiguous bundles remain explicit holds.

October 26: cleanup coordinates script-driven review/archive selections across
both galleries by exact SHA-256 and byte length, even for renamed matches. DELETE
checks both galleries for every file type, regardless of the initiating task's
gallery. Deferred matching actions remain pending. Manual provider changes are
not watched. Duplicate reduction still retains a keeper in each gallery.
Use `photos.py cleanup --output NEW-RUN --apply` for the coordinated workflow.
Scoped folder processors and legacy archive/organization tools are not a general
cross-gallery action service; their existing scope and holds still apply.

To install this update after the active photo job finishes, run
`./deploy-gallery-actions.ps1` from this source checkout. It verifies the copied
files and stops if a photo job is still running; it does not start cleanup.

Apple Private delivery holds report the specific failed operation, elapsed time,
and subprocess timeout when applicable. A pre-upload hold means no upload began;
an uncertain provider write must be verified before retrying.

October 24, 2026: Steve removed automatic media backup retention. Ask before
adding storage/retention requirements. Imports, folder processing, review moves
and RESTORE verify actual destinations and record receipts without keeping
extra backup copies. DELETE now verifies exact current bytes and uses the Recycle Bin, with durable
receipts and no retained backup. Other legacy backup-dependent commands report
holds until converted; never recreate a backup store to bypass a hold.

## Start here

On Steve's current PC, open PowerShell and paste:

```powershell
Set-Location 'C:\PhotoTools\photos'
$python = 'C:\PhotoTools\runtime\Scripts\python.exe'
$runs = 'C:\PhotoTools\runs'
function New-PhotoRun($task) { Join-Path $runs "$task-$(Get-Date -Format yyyyMMdd-HHmmssfff)" }
```

For another installation, follow [one-time setup](SETUP.md) and use its startup block.
Replace example source paths below with your folder or file. Close digiKam before
moving files. Keep OneDrive and iCloud running.

Each processing command checks RESTORE, DELETE and NSFW\CONFIRMED first. New searches
start with a fresh inventory; stopped scans are not automatically resumed.

NSFW checks target personal sexual pictures, including explicit activity and erotic posing.
Nonsexual nudity, family baby photos, bathing and breastfeeding are not NSFW.
Supported junk, memes and downloaded TikTok content skip NSFW inference.
Missing camera metadata alone does not prove an image came from the internet.
Order: exact duplicates, supported junk/memes/TikTok, NSFW score, then remaining
quality checks. Scores at or above the configured threshold (default 0.5) require
actual visual inspection before routing or delivery. Junk exits before NSFW inference.
A high score creates a pending review, never an NSFW decision. Inspect the actual
full image and record its context; another detector or CLIP score is not review.
Only a visually supported personal sexual picture enters NSFW. Ordinary family,
bath, breastfeeding and medical pictures clear this check. Unresolved images stay
held in place, listed in `nsfw-needs-review.csv`; they cannot import or reconcile.

To review every picture already in NSFW, including previous false positives:

```powershell
$run = New-PhotoRun nsfw-visual
& $python visual_nsfw_review.py prepare --folder 'C:\Users\steve\OneDrive\processed_files\NSFW' --output $run
# Reviewer displays each full image and all additional image resources in memory,
# then records actual observations in reviews.json. Never infer these from scores.
& $python visual_nsfw_review.py apply --manifest "$run\manifest.json" --reviews "$run\reviews.json"
```

The evidence format and memory-only rendering command are in
[visual review reference](VISUAL_NSFW_REVIEW.md). Restores verify SHA-256 and byte
length at recorded origins, or dated Camera Roll when origins are unavailable.
Memes move to `FOR_REVIEW\Memes and cartoons`. Videos, unresolved checks and
companions remain held. CONFIRMED/DELETE subfolders are excluded. No other inboxes,
gallery cleanup, or automatic backups run. Visual classification is not CONFIRMED
approval for private delivery.

## Process any folder into one gallery

Check NSFW, blur/quality, memes, receipts, screenshots, other junk, TikTok,
and exact duplicates. Passing files move into the selected gallery's year/month
folders. NSFW goes to `processed_files\NSFW`; other flagged files go to
`processed_files\FOR_REVIEW`. Only exact duplicate inputs are deleted after
their keeper verifies by current SHA-256 and byte length. Errors and uncertain/unsupported
files remain in the source folder and are listed in `report.md`.

```powershell
& $python photos.py process-folder --folder 'C:\Your\Source Folder' --output (New-PhotoRun folder) --apply
```

The default gallery is Personal OneDrive Camera Roll. To select another ordinary
file gallery, add `--gallery 'C:\Your\Gallery'`. Omit `--apply` to preview.
Duplicates use the Recycle Bin by default; add `--delete-mode permanent` to
reclaim their source space immediately. No additional backups are retained.
Review folders use CONFIRMED/RESTORE as usual.

Captioned memes and supported downloaded game/web banners go to review, not
automatic deletion. Memes use `FOR_REVIEW\Memes and cartoons`; web banners use
`FOR_REVIEW\Internet graphics`. A download or missing camera metadata alone does
not make a file junk. Updating filters does not reprocess existing gallery files;
run a requested cleanup to apply them.

## Import into iCloud

### Compare CLIP accuracy and speed

Run `benchmark_clip_accuracy.py --manifest clip_accuracy_labels.json --output RUN/accuracy.json`
with the configured Python runtime. This read-only diagnostic compares CPU and GPU
against explicitly inspected labels and measures the final junk policy, not just
preliminary CLIP rankings. Images remain in memory; only metrics are saved.
The checked-in small spot-check manifest is not a representative validation set.
Different scores alone do not establish an accuracy regression. Do not describe
numerical parity tests as accuracy tests or model scores as probabilities.

October 25: imports deliver only to iCloud. For another source folder, use
`photos.py import --folder "C:\temp\Amazon Photos" --output NEW-RUN --apply`.
Current iCloud bytes are checked for exact duplicates. Local delivery does not
prove cloud synchronization. The iPhone handles copying to OneDrive.

For a reviewed subset, add `--files-from PATH` with a JSON list of absolute source
file paths. The importer freshly inventories association metadata and refuses a
selection that splits a supplied bundle. Unselected files stay untouched.

### Standard drop folder

October 24 import preference: keep stills only from imported Live Photos.
Screen each still for duplicates, NSFW, junk and quality. Import or route the
still to its normal review folder, verify its retained bytes, then recycle its
matching imported motion. Existing matching gallery stills are reused. Match
embedded pairing IDs; filenames alone never justify deleting motion.

Motion without a verified, unambiguous still goes to
`processed_files/FOR_REVIEW/Live Photos`. This folder is created when needed.
Ordinary videos retain their normal checks and import behavior. Existing gallery
Live Photos are unchanged. No extra app or backup is required. The local setting
`import_live_photos: "stills_only"` enables this behavior for imports only.

Inventory/hash and destination comparison happen before inference. Motion that
will be discarded or reviewed does not require video model inference. Completed
still checks are reused only with current identical bytes and compatible rules.

To restart an interrupted import with its completed screening, explicitly use
`photos.py import --resume-from C:\PhotoTools\runs\PRIOR-RUN --output NEW-RUN --apply`.
This freshly inventories and hashes the source and iCloud gallery. Unchanged
completed checks are reused; failed checks are retried and uncertainty stays
uncertain. Complete screened groups can import before failed videos are retried.
If deployed screening files differ from the approved resume configuration, the
run stops before repeating checks. Deploy files without changing line endings.
Create `stop.request` in the active run folder to stop between verified groups;
completed action receipts remain. Reports refresh every 15 seconds, while each
file mutation still has an immediate durable journal entry.

Drop files into `C:\Users\steve\OneDrive\Pictures\IMPORT`, then run:

```powershell
& $python photos.py import --output (New-PhotoRun import) --apply
```

This checks NSFW, memes, receipts, screenshots, other junk, TikTok, blur/quality,
and exact duplicates. Accepted files keep their names and get a verified copy in the local iCloud gallery.
The iPhone supplies OneDrive copies; the importer does not write to OneDrive. Existing exact copies
are reused. Only then is the IMPORT copy recoverably removed.

NSFW goes to `processed_files\NSFW`; junk/TikTok/quality findings go to
`processed_files\FOR_REVIEW`. Use CONFIRMED or RESTORE as usual. RESTORE delivers
wanted review files to their recorded origins and remembers your keep. Failed or
uncertain checks require diagnosis and remain in IMPORT. A processing error is
not a review nomination, a keep decision, or permission to move a file.

Completion means IMPORT is empty, including support files. If anything remains,
ask Steve about each file or a group with the same issue, including whether the
scripts should change. Do not silently archive, waive checks, or call a partial
import complete. Record the chosen disposition before continuing.

October 17: Steve chose `IMPORT\PENDING` for files blocked by unavailable iCloud
comparison bytes. This queue stays deferred and is excluded from automatic import
scans. Reports count active and deferred files separately; pending files are not
successful imports. After Steve chooses this action, use:

```powershell
& $python photos.py import --defer-cloud-blocked 'C:\PhotoTools\runs\PRIOR-RUN' --output (New-PhotoRun import-pending) --apply
```

This moves only that run's cloud-blocked inputs after current content verification,
preserves subfolders, and refuses changed files or naming collisions. Move selected
files out of PENDING into active IMPORT when ready to retry.

If Steve explicitly chooses a JPEG as a still photo despite its Live Photo ID,
add `--still-photo-sha256 EXACT-HASH` for each approved file. This run-specific
decision preserves the original bytes and metadata, requires fresh exact content,
and never splits supplied video/sidecar companions. Duplicate checks, iCloud
verification and cloud holds still apply. It does not reconstruct motion.

The legacy `preserve` import setting still requires native paired cloud proof.
Steve's selected `stills_only` setting removes that requirement for imports.
Matched motion retirement has a durable receipt linking the freshly verified
retained still, so interruption cannot turn an unverified copy into completion.
A video index tag without an association ID does not by itself identify a Live Photo.
Video decoding uses the bundled FFmpeg when OpenCV selects attached cover art or
cannot read a requested sample. The fallback excludes artwork and verifies the
video stream before using its actual final frame. Failed checks, unavailable
comparisons, genuinely incomplete Live Photos/sidecars, missing native
Apple assets, and unsupported Apple formats stay in IMPORT with reasons in
`report.md` inside the new run folder. Local verification does not prove cloud
sync completion.

Video screening decodes forward and batches fallback frame extraction while
keeping the same sample timestamps, full-frame resolution, and five-minute file
deadline. No video frames are saved in the project. Decode failures remain holds.

When local video screening remains uncertain, inspect every unresolved full frame.
Explicit `--visual-assessments PATH` findings can resolve ordinary context for
that run only. The importer verifies the exact content and complete sample
coverage, retains the model result in `assessed-intake.json`, and never creates
a permanent keep. Errors, positive findings and uninspected frames remain holds.

## Merge folders using only exact duplicates

This moves every file type into Camera Roll while preserving source subfolders.
It checks current bytes against the gallery and previously moved files in this
run. Exact duplicate source files go to the Recycle Bin. Conflicting filenames
get readable numbered suffixes. No NSFW, junk, quality, receipt, screenshot or
TikTok filters run, and Apple Photos is untouched.

```powershell
& $python merge_only_duplicates.py --source 'C:\Your\Source Folder' --destination 'C:\Users\steve\OneDrive\Pictures\Camera Roll' --output (New-PhotoRun duplicate-merge) --apply
```

Repeat `--source` for additional folders; they are moved in the supplied order.
Unavailable comparisons stay pending in `summary.json`; originals stay put.

## Reset old processing history

Stop all photo jobs first. This deletes old inventories, reports, decisions,
exclusions, model-result caches, and generated review evidence. It preserves
scripts, settings, models, sign-in sessions, media, and recovery copies. Empty
operational ledgers are initialized so the next scan starts without prior decisions.
Exact original restore locations are forgotten; unknown RESTORE files use the
normal dated Camera Roll fallback.

```powershell
& .\reset_history.ps1 -ProjectRoot 'C:\Users\steve\OneDrive\Documents\ChatGPT\Photo Cleanup' -Apply
```

## Clean both galleries

Video junk and quality checks now inspect decoded samples using the same local
category-evidence policies as photos. Junk results retain timestamped evidence.
Quality nominations require the same extreme exposure problem in every sample;
blur metrics remain diagnostic. Frames stay in memory. Sampling does not review
every intervening frame or audio, and failed checks remain pending.

Search iCloud Photos and Personal OneDrive Camera Roll together. Freshly hash both
galleries, remove exact extras within each, and check each unique photo/video once.
An exact match shares the same NSFW, junk, TikTok and quality result regardless of
filename. Review findings leave both galleries, with one review copy and all
original locations recorded. Retained OneDrive photos/videos missing from iCloud
are copied to iCloud and verified. This workflow never copies iCloud-only files
to OneDrive; the iPhone already supplies that direction independently.

```powershell
& $python photos.py process-gallery --output (New-PhotoRun cleanup) --apply
```

NSFW uses `processed_files\NSFW` and its existing CONFIRMED workflow. Other
findings move into named subfolders of `processed_files\FOR_REVIEW`, such as
Screenshots, Receipts, Memes and cartoons, TikTok, and Blurry or low detail.
A file with multiple flags gets one primary reason folder; its other findings
remain in the report. Exact duplicates are automatically recycled after their
keeper verifies. Verified duplicate recycling uses a current same-gallery keeper,
the Recycle Bin, and durable receipts; it creates no backup or empty review folder.
Photos and videos keep one copy per gallery.

After cleanup, accepted OneDrive files missing from iCloud are copied there,
and their tracked paths are freshly verified. Review/private files, incomplete
Live Photos, unavailable bytes and failed checks stay excluded or pending.
Read `cleanup\report.md` and `cleanup\sync\summary.json` in the run folder.
Local verification does not prove cloud completion.

To cancel a weak ordinary review and reassess everything in FOR_REVIEW:

```powershell
& $python recheck_review.py --output (New-PhotoRun recheck)
```

The analysis starts only when the restore report has no holds. This restores
all recorded original locations and clears restored review files, without marking
them kept. RESTORE still means a permanent keep decision when you choose it yourself.

## Automatic recovery

Normal cleanup runs without ChatGPT. It keeps the computer awake while running, checks real model/OCR execution first,
and retries startup failures with backoff. File processing and copies have a five-minute maximum; stalled files become explicit holds and other work continues. A stalled file never restarts the gallery scan. Completed moves and backups survive a stopped scan; reconciliation uses verified inventory results and holds incomplete coverage. Use a new run folder for each new scan; existing attempts are never silently overwritten.
The display can turn off. Locked Vault and Apple sign-in holds remain pending;
they do not block ordinary gallery cleanup. User review is still required.

Read `supervisor.json` and `report.md` in the run folder. Progress distinguishes
attempted files, fully checked files, failed files and unavailable files.
The active attempt contains its cleanup report and historical delivery report
at `cleanup\sync\prior-delivery-resolution.csv`. Missing/cloud-only bytes stay
pending; old upload receipts are never treated as proof or permission to repeat.
Check progress without ChatGPT, using your run folder:

```powershell
& $python photos.py status --output 'C:\PhotoTools\runs\cleanup-YOUR-RUN-TIMESTAMP'
```

Use the stop command below with the main run folder to cancel all retries.

## Stop a gallery scan

Save the run folder before starting so you can stop it from another PowerShell window:

```powershell
$cleanupRun = New-PhotoRun cleanup
& $python photos.py process-gallery --output $cleanupRun --apply
```

In the other window, use the startup block above, then paste the same run path:

```powershell
& $python photos.py stop --output 'C:\PhotoTools\runs\cleanup-YOUR-RUN-TIMESTAMP'
```

Stopping cancels queued work, lets a move already underway finish safely, and
discards partial scan records. Completed move/restore receipts and backups stay
available. Restart later with a new run folder. This also stops `nsfw-search`
and `pass` runs; use their run folder.

## Adjust scan speed

The default chooses workers using available memory and processor count. To try
six inference workers with two model threads each:

```powershell
& $python photos.py process-gallery --output (New-PhotoRun cleanup) --workers 6 --model-threads 2 --apply
```

Use `progress.json` in the run folder for current counts, queues, memory and
early moves. See [performance results and tuning](PERFORMANCE.md) for the generated-file
benchmark. More workers are not always faster.

## Search both galleries for NSFW

Remove exact within-gallery extras first, then check each distinct content once
across both galleries. Attempt cloud-original downloads and move flagged files into `processed_files\NSFW` for your review.

```powershell
& $python photos.py nsfw-search --output (New-PhotoRun nsfw) --apply
```

Read `nsfw-search-report.md` in the new run folder. Videos use sampled frames;
files that cannot be downloaded or checked are listed as pending.

## Process a folder into both galleries

Check NSFW, junk, and duplicates; move NSFW into review; sort accepted files by
year/month in Camera Roll and deliver eligible originals to iCloud Photos.

```powershell
& $python photos.py process-gallery --folder 'C:\Users\steve\OneDrive\Pictures\Camera Roll\Incoming' --output (New-PhotoRun intake) --apply
```

Use a folder inside Camera Roll. Incomplete Apple Live Photos stay pending.

## Search one Camera Roll folder for NSFW

```powershell
& $python photos.py nsfw-folder --folder 'C:\Users\steve\OneDrive\Pictures\Camera Roll\2026\10' --output (New-PhotoRun nsfw-folder) --apply
```

Review the moved files in `processed_files\NSFW` and read `review-report.md` in
that run folder.

## Send confirmed NSFW to Apple Private and OneDrive Vault

Confirmed NSFW delivery always includes Apple Private and OneDrive Personal Vault,
even when indexing and ordinary cleanup are limited to OneDrive. Open Personal
Vault to start unlocking it; complete any authentication prompt yourself.

1. Move approved files from `processed_files\NSFW` into its `CONFIRMED` folder.
2. Unlock Personal Vault, then run:

```powershell
.\privacy-review.ps1 -PythonPath $python
```

3. Sign into Apple when prompted and approve Windows elevation if requested.
4. On your iPhone, open the Apple **Private** album and choose **Hide**.

The OneDrive destination is **Personal Vault\private**. Verified duplicates are
reused and completed CONFIRMED files are cleared. Files from outside the galleries
can also be placed in CONFIRMED. Apple Private alone does not hide them.

To retry Vault transfers after unlocking it:

```powershell
.\privacy-review.ps1 -PythonPath $python -Resume
```

This finishes transfers; it does not resume a gallery scan.

## Count why files are in FOR_REVIEW

Use the completed cleanup's `files.jsonl`:

```powershell
& $python photos.py review-reasons --scan 'C:\PhotoTools\runs\cleanup-YOUR-RUN\attempt-01\files.jsonl'
```

This hashes the current review files and reports their recorded classifier flags,
weak matches and linked companions. Read `C:\PhotoTools\runs\review-reasons.json`.
It does not run models, move files or process decision folders.

## Restore a photo you want to keep

Move its review file into `C:\Users\steve\OneDrive\processed_files\RESTORE`, then run:

```powershell
& $python photos.py restore
```

It restores every recorded original location. If the original location is unknown,
it sorts the file into OneDrive Camera Roll by year/month. It preserves filenames,
verifies the restored destinations, clears RESTORE after verification and remembers
your keep. Occupied locations with different bytes remain pending.

## Delete a reviewed file from both galleries

Move its review file into `C:\Users\steve\OneDrive\processed_files\DELETE`.
Run any processing command, or run:

```powershell
& $python photos.py review-decisions --apply
```

The tool freshly checks both galleries and recycles every exact matching copy.
It keeps recovery copies and clears DELETE only after local removal verifies.
Unavailable originals and incomplete Live Photo bundles remain pending. Check
`C:\PhotoTools\runs\review-delete-report.json` for counts and holds. Provider
sync must finish before remote deletion can be confirmed. NSFW keeps its separate
CONFIRMED workflow.

DELETE requests persist: later phone uploads of the same exact content are removed
on the next processing run. Returned copies of an open review join that review;
they are not another review decision. Missing review files remain a hold.

Use DELETE for mirrored removal. Deleting directly in the iCloud/OneDrive app or
Explorer is not observed as an intentional deletion by these scripts. Missing
paths can also mean placeholders or sync delays; they never authorize deletion.
There is no background cloud-event watcher. Matching covers exact SHA-256 plus
byte length, not visually similar photos or differently encoded versions.

## Move findings from an already completed cleanup into review

Use the completed attempt folder reported by the cleanup:

```powershell
& $python photos.py review-pending --run 'C:\PhotoTools\runs\cleanup-YOUR-RUN\attempt-01' --apply
```

This freshly verifies the selected files and moves eligible findings to FOR_REVIEW
without another gallery crawl or model scan. One review file represents exact
copies, with Personal OneDrive preferred. Read `review-nominations\summary.json`
for moved originals and held bundles. Use RESTORE to keep or DELETE to remove.

To confirm current preservation and original-path removal without another scan:

```powershell
& $python photos.py review-pending --run 'C:\PhotoTools\runs\cleanup-YOUR-RUN\attempt-01' --verify
```

Read `review-nominations\verification.json`. Returned iCloud placeholders remain
pending; a successful local move does not prove cloud removal.

## Remove extra duplicates and keep one copy in each gallery

```powershell
$dupeRun = New-PhotoRun duplicates
& $python photos.py duplicates --output $dupeRun --download --delete-extras --duplicates-only --apply
& $python photos.py duplicates --output $dupeRun --download --verify
```

Exact photo and video extras within each gallery go to the Recycle Bin automatically.
One copy stays in that same gallery. This legacy executor currently holds rather
than creating a backup; conversion to destination-only verification is still required.
Your keep decisions protect the retained content, not redundant copies. No human
duplicate review is needed. Matching files across iCloud and OneDrive remain.
Unavailable bytes and incomplete Live Photos stay pending. Read `summary.md` in `$dupeRun`.
Omit `--duplicates-only` to also repair verified missing copies between galleries.

To undo that duplicate batch:

```powershell
& $python photos.py duplicates --output $dupeRun --download --undo
```

## Check whether iCloud and OneDrive match

## Trigger downloads of pending iCloud files

Copy freshly inventoried iCloud placeholders to Coded Creations Temp to request
their original bytes. Keep iCloud originals, verify every copy, then delete only
the verified temporary copy:

```powershell
& $python copy_pending_icloud.py 'C:\Users\steve\OneDrive - Coded Creations\Temp' (New-PhotoRun icloud-copy-trigger) --delete-temp
```

Read `progress.json` and `copies.jsonl` in the run folder. Existing Temp files
are never overwritten. Repeated provider failures stop new requests and leave
remaining files pending. This is a download request, not a gallery cleanup.

## Compare and reconcile the two galleries

```powershell
& $python photos.py audit --output (New-PhotoRun audit)
```

To copy verified missing items and check again:

```powershell
& $python photos.py sync --output (New-PhotoRun sync) --apply --settle-seconds 30
```

Unreadable originals and incomplete Live Photos remain pending. Local checks do
not prove that cloud synchronization has finished.

## Remove extra stills beside complete Live Photos

Create a fresh scan, then apply its verified Live Photo findings:

```powershell
$liveRun = New-PhotoRun live-photos
& $python photos.py pass --checks duplicates live-photos --output $liveRun
& $python photos.py live-photo-cleanup --from-scan "$liveRun\files.jsonl" --output "$liveRun\removal" --apply
```

Complete Live Photos are kept. Different edits or unverified matches stay pending.
See [motion-component cleanup](ADVANCED.md#remove-separate-imports-beside-existing-live-photos)
for separately imported MOV files.

## Review blurry or poor-quality photos

```powershell
$qualityRun = New-PhotoRun quality
& $python photos.py quality --gallery onedrive --gallery icloud --output $qualityRun
& $python photos.py collect-quality --gallery onedrive --gallery icloud --scan "$qualityRun\coverage.jsonl"
& $python photos.py move-quality --apply
```

Review `FOR_REVIEW` and `FOR_REVIEW\DUPES`. Put wanted files into RESTORE and
run the restore task above. Quality flags are suggestions for your review.
This moves selected OneDrive originals; copies in the other gallery remain.

To cancel the quality review and restore all its files without marking them kept:

```powershell
& $python photos.py undo-quality-review
```

## Find memes, receipts, screenshots, and reference pictures

```powershell
$junkRun = New-PhotoRun junk
& $python photos.py references --gallery onedrive --gallery icloud --output $junkRun
& $python photos.py collect-references --gallery onedrive --gallery icloud --scan "$junkRun\coverage.jsonl"
```

Review copies appear in `processed_files\FOR_REVIEW`; originals remain in place.
Use the [approved clutter-move instructions](README-stream-review.md) to move or
remove selected originals. For a complete cleanup run, pass your recorded choices:

```powershell
& $python photos.py process-gallery --output (New-PhotoRun cleanup) --decisions 'C:\PhotoTools\runs\my-decisions.jsonl' --apply
```

## Find TikTok downloads

```powershell
$tiktokRun = New-PhotoRun tiktok
& $python photos.py pass --checks tiktok --output $tiktokRun
```

This reports candidates. Follow the [TikTok review and removal steps](ADVANCED.md#identify-tiktok-downloads)
to approve and remove actual TikTok downloads.

## Sort Camera Roll into year/month folders

```powershell
& $python photos.py organize --output (New-PhotoRun organize) --apply
```

## Import an old photo collection into Camera Roll

```powershell
& $python photos.py merge --source 'D:\Photo Archive' --output (New-PhotoRun import) --apply
```

To filter and deliver an incoming Camera Roll folder to both galleries, use the
folder-processing task above.

## Create JPEG copies for upload

```powershell
& $python photos.py jpeg --source 'D:\Photos\picture.heic' --output (New-PhotoRun jpeg)
```

The original is retained. JPEG copies and their manifest go into the new run folder.

## Scan downloaded Amazon photos

```powershell
& $python photos.py pass --gallery amazon --checks nsfw junk quality --output (New-PhotoRun amazon)
```

This produces findings only. Amazon TV collection maintenance is deferred.

## Compare NSFW models on a file

```powershell
& $python photos.py nsfw-model-test --file 'D:\Photos\picture.jpg' --model-dir 'C:\PhotoTools\models\marqo-nsfw-384' --output (New-PhotoRun model-test)
```

Read `report.md` in the new run folder. This comparison does not move photos.

## Measure processing speed

To inspect the active import without processing media or review inboxes:

```powershell
& $python audit_current_run.py --run 'C:\PhotoTools\runs\CURRENT-IMPORT' --output 'C:\PhotoTools\runs\current-import-audit.json'
```

This summarizes completed receipt timings, errors and slow files. The October 27
runtime improvements await a safe deployment after the active import finishes;
see PERFORMANCE.md for measured benefits and accuracy limits.

Compare CPU thread counts and metadata startup using generated test data. This
does not scan your galleries or process review folders.

```powershell
& $python benchmark_photo_runtime.py --model-dir 'C:\PhotoTools\models\marqo-nsfw-384' --exiftool 'C:\Program Files\digiKam\exiftool.exe' --output 'C:\PhotoTools\benchmarks\runtime.json'
```

Read [the performance findings and implementation plan](PERFORMANCE.md).

## Check reviews or find another command

```powershell
& $python photos.py review-status
& $python photos.py tasks
& $python photos.py check
```

For options, add `--help` to a task. Detailed review/repair recipes are in
[ADVANCED.md](ADVANCED.md); capability gaps are in [CAPABILITIES.md](CAPABILITIES.md).

## Clean only Personal OneDrive Camera Roll

Run all duplicate, Live Photo, junk, NSFW, TikTok and quality checks with fresh
hashes. Review inboxes are handled first. Flagged files move to the usual review
folders. This does not scan Apple or reconcile galleries.

```powershell
& $python photos.py tool cleanup_one_gallery --gallery onedrive --output (New-PhotoRun onedrive-cleanup) --apply
```

## Reassess all ordinary review files

Blur/low-detail and low-resolution nominations are disabled. Exposure and decode
problems can still be reviewed. Junk and TikTok checks run before quality; junk
findings bypass quality. NSFW screening still runs.

To undo ordinary review without marking files kept, use the current review run
that contains original-path receipts. Unknown origins go to dated Camera Roll:

```powershell
& $python restore_all_review.py --receipt-run 'C:\PhotoTools\runs\YOUR-REVIEW-RUN' --output (New-PhotoRun restore-review)
```

Wait for `review_reset_complete: true` and no holds in its `summary.json`, then
run the normal both-gallery cleanup recipe above. NSFW is a separate workflow.

## Sort Personal OneDrive into Year\Month

After cleanup, sort remaining photos/videos with their original filenames:

```powershell
& $python sort_onedrive.py --output (New-PhotoRun sort)
```

This previews the plan. To perform the moves, add `--apply`. Only Personal
OneDrive Camera Roll is sorted; Apple is untouched. Capture metadata is preferred,
then dates in filenames. Unknown dates stay in place; add `--undated mtime` only
if you want file-modified dates used as a fallback. Conflicting names get readable
numbered suffixes. Moves preserve verified recovery copies outside synced folders.
To undo a sorting run, use its same output path with `--rollback`.

Junk review now includes supported menus and store price tags as well as memes,
receipts, screenshots and TikTok. Category-specific OCR plus visual evidence is
required. Repair references, family photos and sensitive records remain protected.

Live Photo preference (October 17): keep a complete Live Photo over a separate matching still. Its own picture and video remain together. A unique standalone picture does not require a companion video to import.

## Visual duplicate cleanup

For recompressed copies like the same photo saved twice, follow [visual duplicate cleanup](VISUAL_DUPLICATES.md). This checks both galleries independently and keeps the better encoding.

## Resume Amazon holds (October 25)

Run `run_amazon_import_20261024.py --resume-from PRIOR-RUN --review-holds --convert-formats`.
The source is freshly inventoried and hashed. Compatible screening is reused.
Uncertain NSFW scores remain held in place until actual visual inspection.
Prior-deletion conflicts go to FOR_REVIEW/Import decisions; their exclusion remains
until an explicit RESTORE/keep decision. Real processing failures remain held.
Review wanted files by moving them to RESTORE. A later requested import processes
RESTORE and delivers the approved originals. Do not delete originals from review
as a substitute for an explicit DELETE decision.

With explicitly authorized conversion, single-frame WebP/ORF files retain their
exact originals in OneDrive and receive rendered PNG copies in Apple. PNG pixels,
orientation and alpha are verified; original metadata is preserved in the original.
Conversion receipts distinguish original and rendered hashes. Animated/multiframe
sources and unresolved prior conversion publications stay held. Local destination
verification does not establish cloud upload. Temporary render files are removed.
No automatic backups or retention copies are created.

## Correct a false NSFW flag

Move wanted files into `C:/Users/steve/OneDrive/processed_files/NSFW/SFW`.
The next normal processing run restores them to recorded origins (or dated Camera
Roll when unknown), verifies exact bytes, and remembers the safe decision.
To process only this inbox, run `python sfw_feedback.py` from the tools folder.
Pending detector feedback remains visible until Codex reviews the restored content,
checks the previous classification, and records any justified code fix and test.
This is a review queue, not automatic model retraining or an always-on watcher.

## Review videos held for NSFW

The configured local vision model now inspects score-positive images and sampled
video frames immediately. It runs on this PC; images stay in memory. Ordinary
family imagery clears the NSFW flag, supported sexual-content findings go to
NSFW review, and unreadable or genuinely ambiguous checks remain reported.
Model findings never count as a human CONFIRMED decision.

For an already running scan that started before this capability was enabled, run
`python live_nsfw_review.py --run RUN_FOLDER --owner-pid SCAN_PID` once. It follows
the current coverage log, gives new flags priority over the backlog, freshly
hashes reviewed files, preserves completed checks, and uses the existing verified
review movers. Status and results are in `RUN_FOLDER/visual-review`. It finishes
after the source scan ends and its queue is drained. Do not start a second follower.

Use `visual_nsfw_review.py prepare --help` for a fresh inventory, then
`video_visual_review.py prepare --manifest MANIFEST` for full decode verification.
Render with `render --manifest MANIFEST --video ID --start 0 --count 12`.
Use `--count -1` for all sheets as length-prefixed PNG bytes in memory. Do not save
frames in project folders. Inspect every planned sample and provide hash-bound
observations for each video, then use `apply --manifest MANIFEST --reviews REVIEWS`.
Samples include half-second intervals, scene changes and endpoints. Audio and
intervening frames are not visually reviewed; brief events can be missed. Failed
or uncertain reviews remain holds and must be raised with Steve.

## Approved unreadable import review
For an explicitly approved set of unreadable intake videos, save the exact source
rows and authority in a plan, then run `review_unreadable_import.py --plan PLAN
--output NEW_RUN --apply` with the correct `PHOTOS_CONFIG` import folder. This
moves bytes to `FOR_REVIEW/Unreadable videos` only after fresh SHA-256/length
verification, retains original paths and failed screening evidence, and makes no
NSFW determination or keep decision. Local-only fingerprint checks allow at most
five minutes per operation. Changed sources or failed destinations are not removed.

`icloud_png_import.py --plan PLAN --output NEW_RUN --apply` handles the explicitly
approved eight-file archive plan only. WebP stills become PNG with identical pixels
and supported metadata verified; animations and unsupported metadata stay pending.
A mislabeled actual JPEG uses its exact original bytes with `.jpg`. Deliveries go
only to iCloud. `--only-sha256 HASH` scopes a followup to one already approved row.
No original media backups or retained render copies are created. Sources retire
only after verified destination receipts. Check previous receipts before reusing a
plan; missing completed sources do not authorize replay.

The local visual-review server starts with `--cache-ram 0`. Its default otherwise
retains up to 8 GiB of cross-image prompt states alongside detector models. This
setting disables that extra RAM cache only: the same Qwen model/projector, full
image view, 4096-token context, prompts, thresholds and all screening checks remain.
Active-context KV/prefix reuse stays enabled. Exact-content successful screening
results still use the maintained SQLite cache after fresh identity checks.
The October 28 bounded validation completed 24 distinct synthetic full-image
reviews; server working memory stayed near 4.3 GiB without accumulated cache growth.
See `vision-memory-loading-deployment-20261028.json` for measured evidence and tests.

MPO review reads every indexed JPEG using its declared offset and size. Only FF
alignment padding is accepted between resources or after the final resource.
Unknown bytes, overlaps, truncation and ambiguous offset recovery remain held.
Original files are never rewritten. Parsing validation does not complete screening.

Shared vision startup is serialized across workers. Workers wait for the existing
startup and reuse its healthy server. An occupied unhealthy port is an explicit
runtime hold; failed owned startups are cleaned up before retry. No models or
screening rules change. See vision-startup-lock-deployment-20261029.json.

Two workers share one video decode/screening slot to bound decoder RAM. Photos
can use the other worker. Native resolution and every configured sample/check
remain unchanged. Five-minute file deadlines still apply; slot timeouts remain
failed checks. Exact reviewed scheduling-only code retains compatible successful
results; further source changes invalidate them normally.

