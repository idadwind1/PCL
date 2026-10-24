# Automatic upstream releases

The **Sync upstream and release** workflow checks `Meloong-Git/PCL`'s `main`
branch hourly, at minute 23. It also runs when its automation files are pushed
to this fork's `main`, and can be started from **Actions → Sync upstream and
release → Run workflow**.

Upstream currently publishes source updates without GitHub Releases. The
workflow reads `VersionBaseName` from upstream `ModBase.vb`, builds the Release
configuration, and publishes a tag such as `v2.99.0.0`. It publishes the latest
available source version; it does not backfill older versions or create another
release for changes that keep the same version number.

Each run:

1. Fetches upstream and merges it into this fork's `main`, preserving fork files.
2. Sets exactly one `RestrictMcDownload` declaration to `False` and commits it
   if necessary. A changed/missing declaration or source merge conflict stops
   the run. A conflict affecting only the generated root `最新正式版.zip` keeps
   the fork's archive; it is refreshed from the published fork executable below.
3. Pushes normally, without force, and skips compilation if that version already
   has a published release.
4. Checks out the exact synced commit and its submodules on Windows, restores
   dependencies, and compiles the .NET Framework 4.8 launcher with MSBuild.
5. Uploads the executable, `SHA256SUMS.txt`, and `build-info.json` as an Actions
   artifact. The metadata records both upstream and fork commits.
6. Creates the version tag on the built fork commit, uploads all assets to a
   draft release, and publishes only after all uploads succeed.
7. Downloads that published EXE, verifies its size and GitHub SHA-256 digest,
   and commits an updated root `最新正式版.zip` to `main`. The ZIP contains one
   file, `Plain Craft Launcher 2.exe`, matching the release EXE exactly. The
   release tag remains on the source commit that was compiled.

The archive step also runs when the source version is already published. This
repairs a missing or outdated root ZIP without rebuilding or changing release
assets. If it already contains the correct EXE, no commit or download is needed.
An older run cannot overwrite the archive after `main` changes source versions.

Syncing and building happen in the same workflow. A push made with
`GITHUB_TOKEN` does not start another push workflow, so this pipeline does not
depend on the existing **Build** workflow running afterward.

## Setup

Commit and push the automation files and the download flag change to `main`.
For a new fork, enable workflows on its **Actions** page if GitHub prompts you.
No personal access token is needed: the workflow requests `contents: write`
only for the sync, publish, and archive jobs. Branch protection must permit
their normal pushes to `main`.

Optional Actions secrets preserve the upstream build's credential convention:

- `CLIENT_ID`: your Microsoft OAuth application client ID.
- `CURSEFORGE_API_KEY`: your CurseForge API key.

If set, these are included as defaults in the distributed executable. Runtime
`PCL_MS_CLIENT_ID` and `PCL_CURSEFORGE_API_KEY` still override them. Without
secrets, the defaults remain empty. Enabling Minecraft downloads does not
restore the other removed open-source features or supply API credentials.

The checked-in `Help.zip` is used, so the build uses the help resources belonging
to the selected source commit.

## Failures and retries

- Resolve merge conflicts on `main`, preserving the download flag and automation,
  then run the workflow again. No conflicted merge is pushed.
- Failed builds do not create a tag or release. Run the workflow again after
  fixing the cause.
- Failed uploads leave a draft release. Rerunning retries draft uploads when the
  built commit is unchanged. A version tag on a different commit is rejected;
  it is never silently moved. Inspect that draft/tag before manually removing
  it if a different commit needs to be built.
- Published releases and their assets are left unchanged. Use a new source
  version for a new release.
- If updating the root ZIP fails after publishing, the release stays published;
  rerun the workflow to retry the archive update.
- GitHub may disable scheduled workflows in inactive public repositories after
  60 days. Re-enable the workflow from Actions if that happens.
