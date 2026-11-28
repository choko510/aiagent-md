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

// Interpret indirections only beneath the already-open project directory.
// Never reopen the project by pathname or follow a link during traversal.
fn resolve(base: &[String], value: &str) -> Result<Vec<String>, ()> {
    if value.is_empty() || value.len() > HEAD_LIMIT as usize || value.starts_with(['/', '\\']) {
        return Err(());
    }
    let mut parts = base.to_vec();
    for part in value.split(['/', '\\']) {
        match part {
            "" => return Err(()),
            "." => (),
            ".." => {
                parts.pop().ok_or(())?;
            }
            name if crate::discovery::valid_component(name)
                && !crate::discovery::blocked_component(name) =>
            {
                parts.push(name.to_owned())
            }
            _ => return Err(()),
        }
        if parts.len() > 32 {
            return Err(());
        }
    }
    Ok(parts)
}

fn open_path(project: &Dir, parts: &[String]) -> Result<Dir, ()> {
    let mut dir = project.try_clone().map_err(|_| ())?;
    for part in parts {
        dir = dir.open_dir_nofollow(part).map_err(|_| ())?;
    }
    Ok(dir)
}

pub(crate) fn observe(project: &Dir) -> GitHead {
    let metadata = match project.symlink_metadata(".git") {
        Ok(value) => value,
        Err(_) => return GitHead::Untrusted,
    };
    let result = (|| -> Result<GitHead, ()> {
        if metadata.is_symlink() {
            return Err(());
        }
        let (git, path, pointer) = if metadata.is_file() {
            let raw = read(project, ".git", HEAD_LIMIT)?.ok_or(())?;
            let path = resolve(&[], line(&raw)?.strip_prefix("gitdir: ").ok_or(())?)?;
            (open_path(project, &path)?, path, Some(raw))
        } else if metadata.is_dir() {
            (
                project.open_dir_nofollow(".git").map_err(|_| ())?,
                vec![".git".to_owned()],
                None,
            )
        } else {
            return Err(());
        };
        let common_raw = read(&git, "commondir", HEAD_LIMIT)?;
        let common = match &common_raw {
            Some(raw) => open_path(project, &resolve(&path, line(raw)?)?)?,
            None => git.try_clone().map_err(|_| ())?,
        };
        let raw = read(&git, "HEAD", HEAD_LIMIT)?.ok_or(())?;
        let head = line(&raw)?;
        let hash = if let Some(hash) = oid(head) {
            Some(hash)
        } else {
            let reference = head.strip_prefix("ref: ").ok_or(())?;
            if !valid_ref(reference) {
                return Err(());
            }
            match loose(&common, reference)? {
                Some(hash) => Some(hash),
                None => packed(&common, reference)?,
            }
        };
        // Detect changed indirections during lookup; revalidation is still required before use.
        if read(&git, "HEAD", HEAD_LIMIT)?.as_deref() != Some(raw.as_slice())
            || read(&git, "commondir", HEAD_LIMIT)? != common_raw
            || (pointer.is_some() && read(project, ".git", HEAD_LIMIT)? != pointer)
        {
            return Err(());
        }
        Ok(hash.map_or(GitHead::Unresolved, GitHead::Observed))
    })();
    result.unwrap_or(GitHead::Untrusted)
}
