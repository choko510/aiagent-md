//! Read-only, capability-relative Git ref observation. Never loads Git config.
use crate::discovery::GitHead;
use cap_fs_ext::{DirExt, FollowSymlinks, MetadataExt, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use std::io::{Read, Seek, SeekFrom};

const HEAD_LIMIT: u64 = 1024;
const PACKED_LIMIT: u64 = 256 * 1024;

/// Missing files are distinct from unsafe or unreadable files.
fn read(dir: &Dir, name: &str, limit: u64) -> Result<Option<Vec<u8>>, ()> {
    match dir.symlink_metadata(name) {
        Ok(meta) if meta.is_file() && !meta.is_symlink() => (),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        _ => return Err(()),
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK);
    }
    let mut file = match dir.open_with(name, &options) {
        Ok(file) => file,
        Err(_) => return Err(()),
    };
    let metadata = file.metadata().map_err(|_| ())?;
    // MetadataExt obtains by-handle link counts on Windows. Missing metadata
    // must fail closed, including filesystems which cannot supply a link count.
    let links = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| metadata.nlink()))
        .map_err(|_| ())?;
    if !metadata.is_file() || links != 1 || metadata.len() > limit {
        return Err(());
    }
    let mut first = Vec::new();
    (&mut file)
        .take(limit + 1)
        .read_to_end(&mut first)
        .map_err(|_| ())?;
    if first.len() as u64 > limit {
        return Err(());
    }
    file.seek(SeekFrom::Start(0)).map_err(|_| ())?;
    let mut second = Vec::new();
    (&mut file)
        .take(limit + 1)
        .read_to_end(&mut second)
        .map_err(|_| ())?;
    if first != second {
        return Err(());
    }
    Ok(Some(first))
}

fn line(raw: &[u8]) -> Result<&str, ()> {
    let text = std::str::from_utf8(raw).map_err(|_| ())?;
    let text = text
        .strip_suffix("\r\n")
        .or_else(|| text.strip_suffix('\n'))
        .unwrap_or(text);
    if text.is_empty() || text.chars().any(char::is_control) {
        return Err(());
    }
    Ok(text)
}

fn oid(value: &str) -> Option<String> {
    if matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        && value.bytes().any(|c| c != b'0')
    {
        Some(value.to_owned())
    } else {
        None
    }
}

fn valid_ref(value: &str) -> bool {
    value.starts_with("refs/heads/")
        && value.len() <= 240
        && value.is_ascii()
        && !value.contains("..")
        && !value.contains("@{")
        && !value.ends_with('/')
        && value.split('/').all(|part| {
            crate::discovery::valid_component(part)
                && !part.starts_with('.')
                && !part.ends_with('.')
                && !part.ends_with(".lock")
                && part
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-' | b'.'))
        })
}

fn loose(git: &Dir, reference: &str) -> Result<Option<String>, ()> {
    let mut dir = git.try_clone().map_err(|_| ())?;
    let mut parts = reference.split('/').peekable();
    while let Some(part) = parts.next() {
        if parts.peek().is_none() {
            return read(&dir, part, HEAD_LIMIT)?
                .map(|v| line(&v).ok().and_then(oid).ok_or(()))
                .transpose();
        }
        match dir.symlink_metadata(part) {
            Ok(meta) if meta.is_dir() && !meta.is_symlink() => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            _ => return Err(()),
        }
        dir = dir.open_dir_nofollow(part).map_err(|_| ())?;
    }
    Err(())
}

fn packed(git: &Dir, reference: &str) -> Result<Option<String>, ()> {
    let Some(raw) = read(git, "packed-refs", PACKED_LIMIT)? else {
        return Ok(None);
    };
    let text = std::str::from_utf8(&raw).map_err(|_| ())?;
    let mut result = None;
    for (index, entry) in text.lines().enumerate() {
        if index >= 4096 {
            return Err(());
        }
        if entry.starts_with('#') || entry.starts_with('^') {
            continue;
        }
        let (hash, name) = entry.split_once(' ').ok_or(())?;
        if oid(hash).is_none() || name.is_empty() || name.chars().any(char::is_control) {
            return Err(());
        }
        if name == reference {
            if result.is_some() {
                return Err(());
            }
            result = oid(hash);
        }
    }
    Ok(result)
}

pub(crate) fn observe(project: &Dir) -> GitHead {
    let metadata = match project.symlink_metadata(".git") {
        Ok(value) => value,
        Err(_) => return GitHead::Untrusted,
    };
    if metadata.is_file() {
        return GitHead::Unsupported;
    } // worktree gitdir requires a separately authorized binding
    if !metadata.is_dir() || metadata.is_symlink() {
        return GitHead::Untrusted;
    }
    let git = match project.open_dir_nofollow(".git") {
        Ok(dir) => dir,
        Err(_) => return GitHead::Untrusted,
    };
    let result = (|| -> Result<GitHead, ()> {
        let raw = read(&git, "HEAD", HEAD_LIMIT)?.ok_or(())?;
        let head = line(&raw)?;
        if let Some(hash) = oid(head) {
            return Ok(GitHead::Observed(hash));
        }
        let reference = head.strip_prefix("ref: ").ok_or(())?;
        if !valid_ref(reference) {
            return Err(());
        }
        let hash = match loose(&git, reference)? {
            Some(hash) => Some(hash),
            None => packed(&git, reference)?,
        };
        // Detect a changed HEAD during lookup; still require revalidation before use.
        if read(&git, "HEAD", HEAD_LIMIT)?.as_deref() != Some(raw.as_slice()) {
            return Err(());
        }
        Ok(hash.map_or(GitHead::Unresolved, GitHead::Observed))
    })();
    result.unwrap_or(GitHead::Untrusted)
}
