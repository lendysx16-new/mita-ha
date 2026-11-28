#!/usr/bin/env bash
set -euo pipefail

cd -- "${1:?Pass the deployment checkout path}"
revision=${2:?Pass the validated commit SHA}

if [[ $(git branch --show-current) != main || -n $(git rev-parse --show-prefix) ]]; then
    echo "Deploy from the root of a checkout on branch main." >&2
    exit 1
fi
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
    echo "Commit or remove tracked server changes before deploying." >&2
    exit 1
fi
if [[ ! -r .env ]]; then
    echo "The server checkout needs a readable .env file." >&2
    exit 1
fi

git fetch --no-tags origin refs/heads/main
if [[ $(git rev-parse FETCH_HEAD) != "$revision" ]]; then
    echo "Skipping superseded commit $revision. Deployments only apply the validated tip of main."
    exit 0
fi
if ! git merge-base --is-ancestor HEAD "$revision"; then
    echo "The server checkout has diverged from the validated revision." >&2
    exit 1
fi
git merge --ff-only "$revision"

docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
