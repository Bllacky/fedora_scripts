#create Anydesk repo in repo folder
"[anydesk]\nname=AnyDesk Fedora - stable\nbaseurl=http://rpm.anydesk.com/fedora/x86_64/\ngpgcheck=0\nrepo_gpgcheck=1\ngpgkey=https://keys.anydesk.com/repos/RPM-GPG-KEY" > /etc/yum.repos.d/AnyDesk-Fedora.repo
sudo dnf makecache
#installing dependencies
sudo dnf install redhat-lsb-core
sudo dnf --releasever=32 install pangox-compat.x86_64
sudo dnf install mesa-libGLU
sudo dnf install gtkglext-libs
#fixing some library paths hardcoded in Anydesk that don't match with Fedora
ln -s /lib64/libgtkglext-x11-1.0.so.0 /lib64/libgtkglext-x11-1_0-0.so.0
echo "/usr/lib64/gtk-3.0/modules/" > /etc/ld.so.conf.d/pk-gtk.conf
ldconfig 
#finally installing anydesk
sudo dnf -y install anydesk
#running anydesk
anydesk
