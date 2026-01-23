#!/bin/bash

BOOKMARK="cld_$RANDOM"

cd /workspace/origin
jj workspace add --name $BOOKMARK -r @ /workspace/current

cd /workspace/current
jj bookmark create -r @ $BOOKMARK

/bin/bash

# Cleanup when shell exits
jj workspace forget
