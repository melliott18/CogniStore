# POSIX tier containment and deployment permissions

The POSIX driver uses directory descriptors and no-follow operations to keep
object access inside its configured tier. This requires a filesystem namespace
managed by CogniStore and trusted administrators. It is not a sandbox for
hostile processes running as the CogniStore account or with filesystem
administration privileges.

## Supported trust and mutation model

Run CogniStore under a dedicated, unprivileged service account. The tier root
and every directory below it must belong to the process's effective user ID
and must not have group or other write permission. New directories are created
with mode `0700`, subject to the process umask. Existing directories are
checked, not silently repaired. The private `.cognistore-staging` directory is
reserved and cannot be used as a bucket.

Ancestors of the configured root must belong to the service account or root
and must not be writable by group or other users. A sticky shared ancestor,
such as a root-owned `/tmp`, is allowed only when its child is owned by a
trusted user, so the sticky-directory rule protects the child from other
users. These checks also apply when the configured tier has not yet been
created; constructing a driver and performing missing-object reads, listings,
or deletes do not create the root.

CogniStore may create object directories, publish or replace regular object
files, and delete object files. Other applications using the same service
account must not relocate the tier root or its ancestors, or move an open
subdirectory outside the tier. Directory renames and swaps that keep opened
directories inside the tier remain contained, but operations may fail or act
on the previously opened directory. Success and availability at the current
pathname are not guaranteed across arbitrary in-tier renames. Other
applications must not insert hard links, change ownership or access
permissions, or alter mount topology. Root and other privileged administrators
are trusted to uphold this boundary.

POSIX ownership and mode bits do not fully describe every platform's access
controls. Administrators must also ensure that access-control lists do not
grant other principals the ability to modify these directories, and that
untrusted processes cannot create external hard links to the tier's files.
Using a private `0700` tier root prevents other users from searching its
object paths. The driver does not portably audit ACLs, system hard-link
restrictions, delegated filesystem administration, or all mount aliases. A
deployment that cannot establish these restrictions is unsupported.

For an existing tier, stop its writers before correcting ownership, directory
permissions, ACLs, or mount layout. Use a dedicated directory owned by the
service account with mode `0700`; avoid a shared group-writable import folder.
Import objects through the driver after preparing the tier. Configure one
filesystem per tier rather than mounting additional filesystems below it.

## How containment is enforced

Bucket and key components must be relative and unambiguous. Absolute paths,
empty interior components, `.` and `..` are rejected. The configured base path
is canonicalized at construction. Each subsequent filesystem traversal opens
one directory component at a time from the filesystem root, with
`O_DIRECTORY` and `O_NOFOLLOW`, relative to the preceding directory descriptor.
Ownership and permissions are checked on the opened directories.

The tier root's device and inode identify the accepted root. An existing root
is observed during construction; a missing root is accepted on first use.
A later traversal that observes a different root device or inode is rejected.
Descendants on a different device are rejected. Object access requires regular
files with a single hard link; special files and multiply linked files are
unsupported.

Read, range-write, stat, recursive listing, publication, deletion, and staging
cleanup use the descriptors acquired by that traversal. Leaf opens also use
`O_NOFOLLOW`, and metadata is obtained without following symbolic links.
Listing does not recurse through symlinks. A directory-to-symlink swap either
fails the no-follow operation or leaves the operation attached to an already
opened directory; it cannot redirect a later pathname lookup through the
replacement symlink. The driver does not reopen a checked absolute object
path to perform the operation.

Staging files are created exclusively inside the private staging directory.
Publication uses source and destination directory descriptors; no-overwrite
publication retains the atomic hard-link operation. Permission preservation
and file durability barriers act on the opened staging file. Namespace
durability barriers and cleanup act on the retained directory descriptors.

These properties follow the POSIX definitions of descriptor-relative
[openat](https://pubs.opengroup.org/onlinepubs/9799919799/functions/open.html),
[renameat](https://pubs.opengroup.org/onlinepubs/9799919799/functions/rename.html),
and [unlinkat](https://pubs.opengroup.org/onlinepubs/9699919799/functions/unlink.html).

## Residual limits

A directory descriptor refers to the same directory after that directory is
renamed. If a trusted or privileged process moves an already opened directory
outside the configured root, subsequent descriptor-relative operations can
still act on that moved directory. Portable POSIX APIs do not atomically
combine a current-ancestry check with publication or deletion. Rechecking a
pathname would introduce another check/use race. This is why directory
relocation is excluded by the deployment boundary, rather than described as a
guarantee of `O_NOFOLLOW`.

Root identity records retain device and inode numbers; they do not retain an
open root descriptor between operations. A filesystem could reuse an inode
after a root is removed, so identity comparison does not establish a permanent
directory identity or authorize root replacement. Root relocation and
replacement remain prohibited by the deployment boundary.

The same limitation applies to a file already open when a trusted process
relocates it. The driver cannot revoke access to an opened inode merely
because its name changes. Linux documents the stability of directory
descriptors across renames in its
[openat rationale](https://www.man7.org/linux/man-pages/man2/openat.2.html).

Device checks reject ordinary cross-filesystem traversal; they cannot identify
every same-device bind mount or filesystem alias. Single-link checks reject
ordinary hard-link aliases when observed, but cannot stop a process with the
necessary permissions creating a link after that check. Hard-link permissions
vary across systems; Linux, for example, has a configurable
[`protected_hardlinks` restriction](https://www.man7.org/linux/man-pages/man5/proc_sys_fs.5.html).
External link creation, ACL changes, mount manipulation, and filesystems that
misrepresent normal POSIX semantics remain outside the supported boundary.

Object mutation locks coordinate driver instances within one process.
Descriptor containment does not provide cross-process compare-and-delete
transactions or serialize unrelated applications writing the same files.
Deployments must coordinate writers separately when they require those
guarantees.

## Platform support and failures

Support is determined by available primitives, not by treating every platform
with a filesystem as supported. The driver requires directory and no-follow
open flags, descriptor-relative namespace operations and metadata lookup,
descriptor-based directory enumeration, and the relevant file-descriptor
operations. Python exposes platform support through
[`os.supports_dir_fd`, `os.supports_fd`, and `os.supports_follow_symlinks`](https://docs.python.org/3/library/os.html#os.supports_dir_fd).
Missing required facilities cause the driver to fail closed.

Filesystem and syscall failures propagate. The driver does not retry using
ordinary absolute path operations, omit no-follow flags, or acknowledge a
successful durable mutation after a required synchronization failure. A
filesystem must implement the required POSIX operations and durability
barriers correctly; merely exposing their names is insufficient. Unsupported
platforms or filesystems must use another supported storage backend.

Deterministic swap regressions exercise reads, publication, stat, listing,
deletion, and mover cleanup. They check outside sentinels and source retention
as well as error handling, without relying on a probabilistic timing loop.
