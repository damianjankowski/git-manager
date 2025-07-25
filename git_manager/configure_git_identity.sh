#!/bin/bash

if [ -f ".env" ]; then
  echo "Loading .env variables..."
  set -o allexport
  source .env
  set +o allexport
else
  echo ".env file not found. Exiting."
  exit 1
fi

GIT_USER_NAME=$GIT_USER_NAME
GIT_USER_EMAIL=$GIT_USER_EMAIL

ROOT_DIR="$1"

if [ -z "$ROOT_DIR" ]; then
  echo "Usage: $0 <directory>"
  exit 1
fi

echo "Searching for Git repositories in directory: $ROOT_DIR"

find "$ROOT_DIR" -type d -name ".git" | while read -r git_dir; do
  repo_dir=$(dirname "$git_dir")

  echo "Found Git repository: $repo_dir"

  cd "$repo_dir" || continue

  git config user.name "$GIT_USER_NAME"
  git config user.email "$GIT_USER_EMAIL"

  echo "Set git config for $repo_dir:"
  echo "  user.name: $(git config user.name)"
  echo "  user.email: $(git config user.email)"
  echo "-----------------------------------"
done

echo "Finished configuring all Git repositories."
