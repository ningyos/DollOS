# Vendor SDK Drop

Third-party SDKs that don't belong in git history (large binaries, license boilerplate).
Fetch them manually before building.

## Cubism SDK for Java

Used by `DollOS-App` (Android) for Live2D rendering.

Download: https://www.live2d.com/download/cubism-sdk/download-java/

Place the `CubismSdkForJava-X-r.Y.Z.zip` archive at the repo root (it's gitignored)
or extract under `vendor/cubism-java/` for the App build to pick up.
