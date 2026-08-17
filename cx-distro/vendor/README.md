# Appearance vendor lock

This directory holds the immutable third-party artifacts used by the XFCE
Frosted Graphite profile. `appearance-sources.sha256` is checked before every
VM-profile ISO build.

- MacTahoe GTK Theme, MIT, source commit `26a6397583c8bc6302ac2de26cb356eb11190285`.
  Only its reviewed prebuilt `MacTahoe-Dark` GTK3/XFWM/Plank release archive is
  installed; no upstream installer, GNOME shell tweak, or GTK4 override runs.
- Plank Reloaded `0.11.172`, upstream release Debian packages for amd64 and
  arm64. The ISO never enables its PPA.
