# Immich 3.1.0 implementation contract

This implementation targets the fixed `v3.1.0` tag. The importer uses API
uploads rather than parsing CLI logs. It never uses undocumented database
updates or directly accesses Immich managed storage.

## Endpoints

Paths below are relative to the configured `/api` URL.

| Endpoint | Contract used |
|---|---|
| `GET /server/version` | `major`, `minor`, `patch`; exactly 3.1.0 |
| `GET /users/me` | Confirm camera account name or explicit user ID |
| `GET /server/media-types` | `image` / `video` arrays contain extensions, despite their OpenAPI descriptions saying MIME types |
| `POST /assets/bulk-upload-check` | `{assets: [{id, checksum}]}` with hex SHA-1; accept or duplicate reject with existing `assetId`. Only for files whose hash is already known (hash index or manifest pre-check); other files are uploaded directly and the server's own checksum dedup answers `duplicate` |
| `POST /assets` | Streaming multipart: `assetData`, optional `sidecarData`, `fileCreatedAt`, `fileModifiedAt`, `visibility`. No `filename` field: `canUploadFile` checks every file part against `body.filename \|\| file.originalName`, so a `filename` form field parsed before `sidecarData` makes the sidecar fail `isSidecar` with HTTP 400 "Unsupported file type". The asset part's own filename becomes `originalFileName` |
| `GET /assets/{id}` | `id`, `ownerId`, base64 SHA-1 `checksum`, `visibility`, `libraryId`, `isTrashed`, `isOffline`, `exifInfo` |
| `PUT /assets/{id}` | Update only `visibility`, then verify |
| `POST /assets/jobs` | `{assetIds: [...], name: "refresh-metadata"}`; needs `job.create`. Requested only for already-present assets whose `exifInfo` does not match; fresh uploads rely on the extraction Immich queues itself. HTTP 403 is downgraded to a report warning |
| `POST /stacks` | `{assetIds: [primary, ...]}`; the first ID becomes `primaryAssetId`. One stack per RAW + rendered pair and per 360 bundle |
| `GET /stacks/{id}` | `id`, `primaryAssetId`, `assets[]`; confirms membership and primary after creation or on rerun |
| `DELETE /stacks/{id}` | Unstacks without deleting assets; used only to rebuild a stack made solely of this importer's members |

`GET /assets/{id}` also exposes `stack` (`id`, `primaryAssetId`, `assetCount`)
or `null`. The timeline lists only stack primaries, so a RAW (`.dng`) stacked
under its rendered JPEG/INSP, or the two INSV masters stacked under their LRV
proxy, do not appear as separate entries. All members are uploaded with
`timeline` visibility; `archive` is no longer used. Rerun rules: a stack that
already contains every member is accepted unchanged (its primary is not
modified); stacks consisting only of our members are deleted and recreated
with the current primary and full membership; a stack containing foreign
assets is reported as `STACK CONFLICT`. Stack operations need the
`stack.create`, `stack.read` and `stack.delete` API key permissions.

Non-2xx responses are reported as `Immich HTTP <status>: <method> <path>` plus
the server's own `message` field (printable, at most 200 characters); other
body fields and headers are never copied into reports.

Upload returns `{id, status}`, where `status` is `created` or `duplicate`.
On successful new upload, the service associates `sidecarData` as an
`AssetFileType.Sidecar` before returning success. Duplicate upload does not
update an existing sidecar. The API has no sidecar replacement endpoint in this
version; no replacement is attempted.

Required upload timestamps are taken from source filesystem mtime. This is a
transport fallback, not a fabricated capture time: XMP does not inject dates,
and Immich extracts original capture timestamps when available. WAV folder
dates always come from the filename. No timezone is inferred from camera names.

## XMP / Lens

The service merges media, video, and sidecar tags with sidecar precedence by
tag name, then reads `Make` and `Model`. Lens selection is:

```text
LensID ?? LensType ?? LensSpec ?? LensModel
```

It returns null for selected strings starting with `Unknown`.

The importer emits `tiff:Make`, `tiff:Model`, and (when confidently known)
`exifEX:LensModel`. It intentionally does not use `aux:LensID`: real ExifTool
13.59 converts a camera-lens label in that field into
`Composite:LensID = Unknown (...)`. That would override `LensModel` and fail.
Conflicting higher-priority embedded lens tags cause NEEDS REVIEW. Other source
metadata is not overwritten. Camera fields in a generated copy of an existing
source sidecar are standardized; all unrelated XML properties are retained.

The local ExifTool test checks that generated XMP parses into the exact Make,
Model and LensModel with no synthetic LensID. Runtime API checks are still
required: differences in server ExifTool behavior, source camera metadata, or
extraction failure must never silently produce a success report.

The API does not expose a standalone sidecar download/association field in
`AssetResponseDto`. The importer does not claim such a read-back. New sidecar
persistence relies on the successful official upload path; API verification
checks final fields. Identity (owner, checksum, library, visibility) is checked
right after each upload; camera fields are polled for all assets together once
every upload is done, so the server's extraction queue is waited on once rather
than per file. A metadata refresh request is asynchronous: matching fields
are a condition check, not a task-completion receipt. Existing resources are
checked but never receive replacement sidecars.

## CLI observations

`--visibility` and `--json-output` exist. CLI JSON contains `newFiles`,
`duplicates`, and `newAssets`, but stdout is not an exclusively JSON stream.
Explicit symlink files are followed via `stat` and file reads, but the importer
does not need this behavior. The CLI sidecar search actually checks the short
`photo.xmp` name before `photo.ext.xmp` in this tag, unlike current documentation.
Sending an explicit multipart sidecar avoids that ambiguity.

CLI auth uses `~/.config/immich/auth.yml` (`url`, `key`), with compatibility
for older `instanceUrl`, `apiKey` keys. The importer only runs `server-info`;
no CLI upload/delete/watch/album options are invoked or inherited.

## Official source references

- [OpenAPI at v3.1.0](https://github.com/immich-app/immich/blob/v3.1.0/open-api/immich-openapi-specs.json)
- [Upload service, including sidecar association and duplicate cleanup](https://github.com/immich-app/immich/blob/v3.1.0/server/src/services/asset-media.service.ts)
- [Metadata extraction and lens priority](https://github.com/immich-app/immich/blob/v3.1.0/server/src/services/metadata.service.ts)
- [ExifTool server configuration](https://github.com/immich-app/immich/blob/v3.1.0/server/src/repositories/metadata.repository.ts)
- [Media types](https://github.com/immich-app/immich/blob/v3.1.0/server/src/utils/mime-types.ts)
- [Media types endpoint implementation](https://github.com/immich-app/immich/blob/v3.1.0/server/src/services/server.service.ts)
- [CLI options](https://github.com/immich-app/immich/blob/v3.1.0/packages/cli/src/index.ts)
- [CLI upload](https://github.com/immich-app/immich/blob/v3.1.0/packages/cli/src/commands/asset.ts)
- [CLI authentication](https://github.com/immich-app/immich/blob/v3.1.0/packages/cli/src/utils.ts)
- [XMP documentation](https://docs.immich.app/features/xmp-sidecars/)
- [ExifTool LensID conversion](https://github.com/exiftool/exiftool/blob/2200871d9cef988051d2a99d67df3bda6cbb30a8/lib/Image/ExifTool/XMP.pm)
