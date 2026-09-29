# apps/render

Intentionally empty. Phase 10's renderer is not a separate service: it is the
`providers/renderer` package inside the API (`apps/api/src/edutoon/providers/renderer`) - a Pillow
scene compositor feeding FFmpeg - driven by `render_video` jobs in the existing worker. Remotion was
evaluated and not used (its company licence is paid above three people); see `apps/api/README.md`,
"Video rendering".
