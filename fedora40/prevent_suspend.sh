#!/bin/bash

# Setting the path for the directory and file
dir_path="/etc/systemd/sleep.conf.d"
file_path="$dir_path/nosuspend.conf"

# Checking if directory exists; if not, create it
if [ ! -d "$dir_path" ]; then
  echo "Directory $dir_path doesn't exist. Creating now"
  sudo mkdir -p "$dir_path"
  echo "Directory created"
else
  echo "Directory $dir_path exists."
fi

# Checking if file exists; if not, create it
if [ ! -e "$file_path" ]; then
  echo "File $file_path doesn't exist. Creating now"
  sudo touch "$file_path"
  echo "File created"
else
  echo "File $file_path exists."
fi

# Writing the content into the file
echo "[Sleep]
AllowSuspend=no
AllowHibernation=no
AllowSuspendThenHibernate=no
AllowHybridSleep=no" | sudo tee "$file_path"

# Provide feedback to the user
echo "Settings have been written to $file_path"
