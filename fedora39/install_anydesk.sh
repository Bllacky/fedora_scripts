#!/usr/bin/env bash

#create Anydesk repo in repo folder
sudo echo -e "[anydesk]\nname=AnyDesk Fedora - stable\nbaseurl=http://rpm.anydesk.com/fedora/x86_64/\ngpgcheck=0\nrepo_gpgcheck=1\ngpgkey=https://keys.anydesk.com/repos/RPM-GPG-KEY" > sudo /etc/yum.repos.d/AnyDesk-Fedora.repo
#Sometimes installation fails with the Fedora repo due to missing dependencies. In this case, use the CentOS repo
#sudo echo -e "[anydesk]\nname=AnyDesk Fedora - stable\nbaseurl=http://rpm.anydesk.com/centos/x86_64/\ngpgcheck=0\nrepo_gpgcheck=1\ngpgkey=https://keys.anydesk.com/repos/RPM-GPG-KEY" > sudo /etc/yum.repos.d/AnyDesk-Fedora.repo
sudo dnf makecache
#installing dependencies
sudo dnf install redhat-lsb-core
sudo dnf install yum-utils
sudo dnf --releasever=39 install pangox-compat.x86_64
sudo dnf install mesa-libGLU
sudo dnf install gtkglext-libs
#fixing some library paths hardcoded in Anydesk that don't match with Fedora
sudo rm /lib64/libgtkglext-x11-1_0-0.so.0 
sudo ln -s /lib64/libgtkglext-x11-1.0.so.0 /lib64/libgtkglext-x11-1_0-0.so.0
sudo rm /usr/lib64/libgtkglext-x11-1_0-0.so.0 
sudo ln -s /usr/lib64/libgtkglext-x11-1.0.so.0 /usr/lib64/libgtkglext-x11-1_0-0.so.0
sudo echo "/usr/lib64/gtk-3.0/modules/" > sudo /etc/ld.so.conf.d/pk-gtk.conf
sudo ldconfig 
#finally installing anydesk
sudo yumdownloader anydesk
sudo rpm -ihv --force anydesk_*x86_64.rpm --nodeps
sudo rm anydesk_*x86_64.rpm
#running anydesk
#deleting old user any desk folder if it exists
sudo rm -rf $HOME/.anydesk
#Starting Anydesk
anydesk
exit
