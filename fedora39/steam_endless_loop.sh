#!/usr/bin/env bash

#If Steam is stuck in an endless loop of starting up and crashing, it happens mostly due to its new interface.
#The easiest solution is to clean up the local cache of the user
sudo rm -d -R ~/.local/share/Steam
