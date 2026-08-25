#!/usr/bin/env bash
# Shared profile helpers for the incremental XFCE Frosted release path.
# Shell sourced by build-base.sh and build-iso.sh; do not execute directly.

profile_validate() {
    case "${PROFILE:-desktop}" in
        desktop|xfce-frosted) ;;
        *) echo "FATAL: --profile must be desktop or xfce-frosted (got: ${PROFILE:-})" >&2; return 1 ;;
    esac
}

profile_package_file() {
    case "$PROFILE" in
        desktop) printf '%s\n' "${SCRIPT_DIR}/packages-desktop.txt" ;;
        xfce-frosted) printf '%s\n' "${SCRIPT_DIR}/packages-xfce-frosted.txt" ;;
    esac
}

# Keep the established desktop cache name so existing GNOME bases remain valid.
profile_base_stem() {
    case "$PROFILE" in
        desktop) printf '%s\n' 'base-desktop' ;;
        xfce-frosted) printf '%s\n' 'base-xfce-frosted' ;;
    esac
}

profile_verify_sources() {
    local repo="$1"
    [ "$PROFILE" = "xfce-frosted" ] || return 0
    [ -f "${repo}/cx-distro/vendor/appearance-sources.sha256" ] || {
        echo "FATAL: Frosted appearance artifact lock file missing" >&2; return 1;
    }
    (cd "${repo}/cx-distro/vendor" && sha256sum -c appearance-sources.sha256 --quiet) || {
        echo "FATAL: Frosted appearance artifact checksum failure" >&2; return 1;
    }
}

profile_configure_display_manager() {
    local chroot="$1"
    if [ "$PROFILE" = "desktop" ]; then
        chroot "$chroot" bash -c '
            systemctl enable gdm
            systemctl enable NetworkManager
            systemctl enable ssh
            groupadd -rf icebreaker-users
            # Live session only: locked password, never an installed login.
            useradd -m -s /bin/bash -G sudo,icebreaker-users -p "!" ubuntu
            echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu-live
            chmod 440 /etc/sudoers.d/ubuntu-live
            mkdir -p /etc/gdm3
            cat > /etc/gdm3/custom.conf <<"GDMCFG"
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=ubuntu
WaylandEnable=false
[security]
[xdmcp]
[chooser]
[debug]
GDMCFG
        '
    else
        chroot "$chroot" bash -c '
            systemctl enable lightdm
            systemctl enable NetworkManager
            systemctl enable ssh
            groupadd -rf autologin
            groupadd -rf icebreaker-users
            useradd -m -s /bin/bash -G sudo,autologin,icebreaker-users -p "!" ubuntu
            echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu-live
            chmod 440 /etc/sudoers.d/ubuntu-live
            mkdir -p /etc/lightdm/lightdm.conf.d
            cat > /etc/lightdm/lightdm.conf.d/50-icebreaker-autologin.conf <<"LDMCFG"
[Seat:*]
autologin-user=ubuntu
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
LDMCFG
        '
    fi
}

profile_apply_overlay() {
    local chroot="$1" repo="$2" arch="$3"
    [ "$PROFILE" = "xfce-frosted" ] || return 0
    profile_verify_sources "$repo" || return 1

    local distro="${repo}/cx-distro/distro"
    local vendor="${repo}/cx-distro/vendor"
    local xfconf="${chroot}/etc/xdg/xfce4/xfconf/xfce-perchannel-xml"
    local dock_item

    for xfconf_file in xfce4-desktop.xml xfce4-panel.xml xsettings.xml xfwm4.xml xfce4-keyboard-shortcuts.xml; do
        install -Dm644 "${distro}/${xfconf_file}" "${xfconf}/${xfconf_file}"
    done
    install -Dm755 "${distro}/icebreaker-appearance-apply" "${chroot}/usr/libexec/icebreaker/appearance-apply"
    install -Dm644 "${distro}/icebreaker-appearance.desktop" "${chroot}/etc/xdg/autostart/icebreaker-appearance.desktop"
    install -Dm644 "${distro}/picom-frosted.conf" "${chroot}/etc/xdg/icebreaker/picom-frosted.conf"
    install -Dm644 "${distro}/dunstrc" "${chroot}/etc/xdg/dunst/dunstrc"
    install -Dm644 "${distro}/plank-settings" "${chroot}/etc/xdg/plank/dock1/settings"
    for dock_item in "${distro}"/plank-dockitems/*.dockitem; do
        install -Dm644 "$dock_item" "${chroot}/etc/xdg/plank/dock1/launchers/$(basename "$dock_item")"
    done
    install -Dm644 "${distro}/icebreaker-frosted-graphite-wallpaper.jpeg" \
        "${chroot}/usr/share/backgrounds/icebreaker-frosted-graphite-wallpaper.jpeg"
    install -Dm644 "${vendor}/MacTahoe-LICENSE" \
        "${chroot}/usr/share/doc/icebreaker/third-party/MacTahoe-LICENSE"
    install -Dm644 "${vendor}/MacTahoe-Dark.tar.xz" "${chroot}/tmp/MacTahoe-Dark.tar.xz"
    install -Dm644 "${vendor}/plank-reloaded_0.11.172_${arch}.deb" \
        "${chroot}/tmp/plank-reloaded.deb"

    chroot "$chroot" bash -c '
        set -e
        # The pinned Plank package declares these as runtime dependencies.
        # Install them explicitly before dpkg so an empty apt cache cannot make
        # `apt-get -f install` silently remove Plank to resolve the transaction.
        apt-get update -qq
        apt-get install -y --no-install-recommends bamfdaemon libgnome-menu-3-0
        dpkg -i /tmp/plank-reloaded.deb
        dpkg-query -W -f="\${db:Status-Status}" plank-reloaded | grep -qx installed
        rm -f /tmp/plank-reloaded.deb
        mkdir -p /usr/share/themes
        tar -xJf /tmp/MacTahoe-Dark.tar.xz -C /usr/share/themes
        rm -f /tmp/MacTahoe-Dark.tar.xz
        apt-get clean && rm -rf /var/lib/apt/lists/*
    '
}
