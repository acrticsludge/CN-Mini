# Edge Cases in Our Distributed Backup System

This section covers eight situations we considered after our D1 submission. These are not things that happen during every backup, but they can cause a file to be lost, left incomplete, or stored incorrectly. We added checks and recovery steps to handle these situations more safely.

## 1. The backup server stops working during a transfer

A server might go down while a file is still being copied. This can leave only part of the file on the server. If the transfer is started again without checking what was received, the backup could become incomplete or incorrect.

Our client keeps track of the last chunk that the server confirmed receiving. When a transfer fails, it reports this point to the backup controller. The controller first tries the same server again from that point. If the server is still unavailable, it switches to the second backup server and starts the transfer again from the beginning. The dashboard also shows the retry or restart, so the user can see what happened.

## 2. The computer sending the backup crashes

The computer sending a file can also shut down unexpectedly, for example if the battery runs out or the system crashes. In that situation, the server may already have received part of the file.

When the user tries again, the dashboard checks for an incomplete file on the backup server. It then gives the user the choice to continue from the chunks already stored or start the transfer again. This avoids sending the same data again when it is not necessary.

## 3. A failed backup should not delete the previous good copy

Imagine backing up a file that already exists on the server. If the server starts writing directly over the old copy and the transfer fails halfway, the original backup could be lost as well.

To avoid this, the server first saves the new transfer as a temporary `.part` file. The existing backup is left as it is while the transfer is in progress. Once all the chunks have arrived and passed the checks, the server replaces the old file with the new one. If the transfer fails, the previous copy remains available. Temporary files are also hidden from the normal file list.

## 4. The backup server runs out of storage or cannot write to disk

A transfer can fail because the backup server does not have enough free space. The disk might also become read-only or return a write error while the file is being saved.

The server checks the available quota before accepting a transfer. It also handles write errors that happen during the transfer and sends a storage error back to the client. The dashboard can send the file to the other backup server if that server has enough space. If neither server has enough space, the transfer is rejected with a clear error instead of being shown as successful.

## 5. Two clients try to back up files with the same name

Two users could send a file with the same name to the same server at almost the same time. Without a check, both transfers might try to write to the same location and the contents could get mixed up.

The server reserves the filename when the first transfer starts. If another client tries to use that filename before the first transfer finishes, the server rejects the second request and reports that the file is busy. The server also reserves the `.part` extension for its temporary files, so a user cannot upload a file that conflicts with one of them.

## 6. The original file is changed while it is being backed up

A user might edit a file while it is being copied. For example, they could change a spreadsheet while the backup is running. This could result in a backup containing a mixture of the old and new contents.

Before sending the file, the client records its size and last modification time. It checks these values again after the transfer. If either value has changed, the client reports that the source file changed and the backup is marked as failed rather than successful.

There is one limitation: an edit that leaves both the file size and modification time unchanged may not be detected by this check. We have kept this limitation clear rather than claiming that every possible edit will be caught.

## 7. The system reports that the backup is complete, but the file is missing

A transfer might report that it has finished even though the file is not actually present in either backup location. Relying only on the completion message could give the user the wrong impression that their data is safe.

After a transfer finishes, the dashboard checks both backup servers for the file and checks whether its size matches what was expected. It then marks the transfer as verified if the copy is found. If it cannot find the file, it shows a verification error instead of treating the backup as a confirmed success.

## 8. Neither backup server can be reached

There may be a time when both backup servers are switched off, disconnected, or otherwise unavailable. In that case, the client should not keep waiting for a long time or leave the user with an unclear error.

The client tries the configured servers one by one, using a short connection timeout for each attempt. If neither server responds, it displays `No backup server reachable` and exits with an error. This normally takes around 20 seconds across both servers, rather than waiting for the longer transfer timeout. Since no connection is made, no partial file is created on either server.
