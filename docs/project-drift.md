# Drift against project files

Create a manifest next to your working files:

```json
{
  "title_file": "title.txt",
  "description_file": "description.txt",
  "tags_file": "tags.json",
  "thumbnail_file": "thumbnail.jpg",
  "metadata": {"default_language": "en"}
}
```

Every mapping is optional, but at least one is required. `metadata` accepts tracked
scalar metadata fields, including title, description, category_id, and language fields.
Tags must be an array of strings. Files override matching inline metadata values.
Paths resolve relative to the manifest, including when invoked from another directory.
One terminal newline is ignored in title and description files; other whitespace is retained.
Tag order and duplicates do not constitute drift.

```bash
ytclaw project link VIDEO_ID ./project.json
ytclaw sync @yourchannel --thumbnails
ytclaw project drift VIDEO_ID
```

The comparison rereads the files every time and uses the latest metadata version with
source `api`. Only mapped fields participate. A YAML import is never used as the
YouTube side. Missing files produce a clear error rather than silently removing fields.
The report is local; it only reflects YouTube through the last sync.

`drifted` describes metadata differences. Thumbnail status is separate:

- `unknown`: no usable archived image or the most recent image attempt failed.
- `bytes_match`: local file and last archived image have identical SHA-256 hashes.
- `bytes_differ`: the bytes differ. YouTube resizing/recompression can cause this even
  when the design is unchanged, so ytclaw does not call it a confirmed design change.

The image comparison includes its check timestamp. A metadata-only sync does not
refresh image evidence. This release does not perform perceptual similarity scoring
or overwrite remote metadata from the files.

`baseline VIDEO_ID` remains useful when there are no working files: it pins a saved
database version. You can use a database baseline and a project link simultaneously.
