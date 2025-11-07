# 1) Hide obviously broken entries
python3 openwith_cleaner.py --fix-broken

# 2) Hide duplicates, preferring native builds (change to flatpak/snap if you want)
python3 openwith_cleaner.py --hide-duplicates --strategy auto --prefer native

# 3) Clean your Open With history/associations file
python3 openwith_cleaner.py --fix-mimeapps

systemctl --user restart xdg-desktop-portal xdg-desktop-portal-gtk
