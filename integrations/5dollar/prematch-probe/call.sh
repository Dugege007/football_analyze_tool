#!/bin/bash
# usage: call.sh <name> <url>   (5DF native or compat host); logs to calls.tsv; sleeps 3s for pacing
name=$1; url=$2
code=$(curl -s -o raw/$name.json -D raw/$name.hdr -w "%{http_code}" -H "Authorization: Bearer $FIVEDOLLAR_FOOTBALL_API_KEY" "$url")
rem=$(grep -i '^x-ratelimit-remaining' raw/$name.hdr | head -1 | tr -d '\r' | awk '{print $2}')
echo -e "$(date +%H:%M:%S)\t5df\t$name\t$code\t$rem\t$url" >> calls.tsv
echo "$name $code rem=$rem"
sleep 3
