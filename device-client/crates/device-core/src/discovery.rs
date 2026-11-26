//! Explicit-root, read-only project discovery. No Git, build, shell or network.
//! A caller must obtain local user consent before opening a root; this module is
//! not exposed to the model or WebView and does not grant execution authority.
use cap_fs_ext::DirExt;
use cap_std::fs::Dir;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeSet, HashMap, HashSet},
    fs::OpenOptions,
    path::{Component, Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const MAX_ENTRIES: usize = 2048;
const MAX_DEPTH: usize = 3;
const MAX_TIME: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum ProjectKind {
    Rust,
    Python,
    Web,
    Android,
    Flutter,
    Tauri,
    Electron,
    Ios,
}

#[derive(Debug, PartialEq, Eq)]
pub struct ProjectCandidate {
    pub candidate_id: String,
    pub generation: u64,
    pub freshness_seconds: u64,
    pub observed_at_ms: u64,
    pub kinds: Vec<ProjectKind>,
    pub git_detected: bool,
    /// No Git process or untrusted configuration is executed for discovery.
    pub dirty_state: &'static str,
}

#[derive(Debug, PartialEq, Eq)]
pub enum GitHead {
    /// A syntactically valid, observed ref value; the object has not been verified.
    Observed(String),
    /// No loose or packed ref was found; unborn and unsupported backends are indistinguishable.
    Unresolved,
    Unsupported,
    Untrusted,
}

pub struct WorkspaceRoot {
    root: Dir,
    nonce: [u8; 32],
    local_paths: HashMap<String, LocalCandidate>,
    generation: u64,
    valid_until: Option<Instant>,
}

struct LocalCandidate {
    path: PathBuf,
    dir: Dir,
}

impl WorkspaceRoot {
    /// Opens the explicitly selected root as an OS directory capability.
    /// Selection/consent and persistent identity are responsibilities of the caller.
    pub fn open_selected(path: &Path) -> Result<Self, &'static str> {
        if !path.is_absolute()
            || path.parent().is_none()
            || path
                .components()
                .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
        {
            return Err("INVALID_WORKSPACE_ROOT");
        }
        if path.components().any(|c| matches!(c, Component::Normal(name) if name.to_str().is_some_and(blocked_component))) {
            return Err("SENSITIVE_ROOT_DENIED");
        }
        let root = open_root(path)?;
        Ok(Self {
            root: Dir::from_std_file(root),
            nonce: crate::security::random_bytes().map_err(|_| "RANDOM_UNAVAILABLE")?,
            local_paths: HashMap::new(),
            generation: 0,
            valid_until: None,
        })
    }

    /// Returns pathless local metadata only. No project content is read or sent.
    pub fn discover(&mut self) -> Result<Vec<ProjectCandidate>, &'static str> {
        self.local_paths.clear();
        self.valid_until = None;
        self.generation = self
            .generation
            .checked_add(1)
            .ok_or("GENERATION_EXHAUSTED")?;
        let mut paths = HashMap::new();
        let mut out = Vec::new();
        let mut count = 0;
        scan(
            &self.root,
            Path::new(""),
            0,
            &self.nonce,
            self.generation,
            Instant::now(),
            &mut count,
            &mut paths,
            &mut out,
        )?;
        let observed = u64::try_from(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| "CLOCK_UNAVAILABLE")?
                .as_millis(),
        )
        .map_err(|_| "CLOCK_UNAVAILABLE")?;
        for candidate in &mut out {
            candidate.observed_at_ms = observed;
        }
        out.sort_by(|a, b| a.candidate_id.cmp(&b.candidate_id));
        self.local_paths = paths;
        self.valid_until = Some(Instant::now() + Duration::from_secs(60));
        Ok(out)
    }

    /// Local display only: never send to Cloud or use as a file-open authority.
    pub fn local_relative_path(&self, candidate_id: &str, generation: u64) -> Option<&Path> {
        if generation != self.generation
            || self
                .valid_until
                .is_none_or(|deadline| Instant::now() >= deadline)
        {
            return None;
        }
        self.local_paths
            .get(candidate_id)
            .map(|candidate| candidate.path.as_path())
    }

    /// Uses the directory handle retained from discovery, not a reopened path.
    /// The result is an observation, never a source-revision or execution grant.
    pub fn probe_git(&self, candidate_id: &str, generation: u64) -> Result<GitHead, &'static str> {
        if generation != self.generation
            || self
                .valid_until
                .is_none_or(|deadline| Instant::now() >= deadline)
        {
            return Err("STALE_CANDIDATE");
        }
        let candidate = self
            .local_paths
            .get(candidate_id)
            .ok_or("UNKNOWN_CANDIDATE")?;
        Ok(crate::git_probe::observe(&candidate.dir))
    }
}

pub(crate) fn valid_component(name: &str) -> bool {
    if name.is_empty()
        || !name.is_ascii()
        || name.ends_with([' ', '.'])
        || name.contains([':', '/', '\\'])
        || name.chars().any(char::is_control)
    {
        return false;
    }
    let stem = name.split('.').next().unwrap_or("").to_ascii_uppercase();
    if matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL") {
        return false;
    }
    if stem.len() == 4
        && (stem.starts_with("COM") || stem.starts_with("LPT"))
        && matches!(stem.as_bytes()[3], b'1'..=b'9')
    {
        return false;
    }
    true
}

fn blocked_component(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    matches!(
        lower.as_str(),
        ".ssh"
            | ".aws"
            | ".azure"
            | ".gnupg"
            | "user data"
            | "chrome"
            | "chromium"
            | "firefox"
            | "brave-browser"
            | "bravesoftware"
            | "microsoftedge"
            | "profiles"
            | "credentials"
    ) || lower
        .strip_prefix("profile ")
        .is_some_and(|suffix| !suffix.is_empty() && suffix.bytes().all(|b| b.is_ascii_digit()))
}

#[allow(clippy::too_many_arguments)] // explicit bounds and output state accompany each directory
fn scan(
    dir: &Dir,
    relative: &Path,
    depth: usize,
    nonce: &[u8; 32],
    generation: u64,
    start: Instant,
    count: &mut usize,
    paths: &mut HashMap<String, LocalCandidate>,
    output: &mut Vec<ProjectCandidate>,
) -> Result<(), &'static str> {
    if start.elapsed() > MAX_TIME {
        return Err("DISCOVERY_TIMEOUT");
    }
    let mut entries = Vec::new();
    let mut names = HashSet::new();
    for entry in dir.entries().map_err(|_| "DISCOVERY_IO")? {
        *count += 1;
        if *count > MAX_ENTRIES {
            return Err("DISCOVERY_LIMIT");
        }
        if start.elapsed() > MAX_TIME {
            return Err("DISCOVERY_TIMEOUT");
        }
        let entry = entry.map_err(|_| "DISCOVERY_IO")?;
        let name = entry.file_name();
        let Some(text) = name.to_str() else { continue };
        if !valid_component(text) {
            continue;
        }
        if !names.insert(text.to_ascii_lowercase()) {
            return Err("CASE_COLLISION");
        }
        let kind = entry.file_type().map_err(|_| "DISCOVERY_IO")?;
        if kind.is_symlink() {
            continue;
        }
        entries.push((name, kind));
    }
    entries.sort_by(|a, b| a.0.cmp(&b.0));
    let mut kinds = BTreeSet::new();
    let mut git = false;
    for (name, file_type) in &entries {
        let text = name.to_str().ok_or("DISCOVERY_IO")?;
        if text == ".git" && (file_type.is_file() || file_type.is_dir()) {
            git = true;
        }
        if !file_type.is_file() {
            continue;
        }
        match text {
            "Cargo.toml" => {
                kinds.insert(ProjectKind::Rust);
            }
            "pyproject.toml" | "requirements.txt" => {
                kinds.insert(ProjectKind::Python);
            }
            "package.json" => {
                kinds.insert(ProjectKind::Web);
            }
            "settings.gradle" | "settings.gradle.kts" | "build.gradle" | "build.gradle.kts" => {
                kinds.insert(ProjectKind::Android);
            }
            "pubspec.yaml" => {
                kinds.insert(ProjectKind::Flutter);
            }
            "tauri.conf.json" | "tauri.conf.json5" => {
                kinds.insert(ProjectKind::Tauri);
            }
            "electron-builder.yml" | "electron-builder.yaml" | "electron-builder.json" => {
                kinds.insert(ProjectKind::Electron);
            }
            _ => (),
        }
    }
    if entries
        .iter()
        .any(|(n, t)| t.is_dir() && n.to_str().is_some_and(|v| v.ends_with(".xcodeproj")))
    {
        kinds.insert(ProjectKind::Ios);
    }
    if !kinds.is_empty() || git {
        let mut hash = Sha256::new();
        hash.update(b"aiagent-local-project-candidate-v1");
        hash.update(nonce);
        hash.update(relative.to_string_lossy().as_bytes());
        let id = crate::protocol::hex_digest(&hash.finalize());
        paths.insert(
            id.clone(),
            LocalCandidate {
                path: relative.to_path_buf(),
                dir: dir.try_clone().map_err(|_| "DISCOVERY_IO")?,
            },
        );
        output.push(ProjectCandidate {
            candidate_id: id,
            generation,
            freshness_seconds: 60,
            observed_at_ms: 0,
            kinds: kinds.into_iter().collect(),
            git_detected: git,
            dirty_state: "UNKNOWN_DIRTY_STATE",
        });
    }
    if depth < MAX_DEPTH {
        for (name, file_type) in entries {
            let Some(text) = name.to_str() else { continue };
            if !file_type.is_dir()
                || text.starts_with('.')
                || blocked_component(text)
                || matches!(
                    text.to_ascii_lowercase().as_str(),
                    "node_modules" | "target" | "build" | "dist"
                )
                || text.ends_with(".xcodeproj")
            {
                continue;
            }
            let child = dir
                .open_dir_nofollow(text)
                .map_err(|_| "DISCOVERY_BOUNDARY_OR_IO")?;
            scan(
                &child,
                &relative.join(&name),
                depth + 1,
                nonce,
                generation,
                start,
                count,
                paths,
                output,
            )?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn open_root(path: &Path) -> Result<std::fs::File, &'static str> {
    use std::os::unix::fs::OpenOptionsExt;
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| "WORKSPACE_ROOT_UNAVAILABLE")
}

#[cfg(windows)]
fn open_root(path: &Path) -> Result<std::fs::File, &'static str> {
    use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
    use std::path::Prefix;
    let Some(Component::Prefix(prefix)) = path.components().next() else {
        return Err("INVALID_WORKSPACE_ROOT");
    };
    let drive = match prefix.kind() {
        Prefix::Disk(letter) | Prefix::VerbatimDisk(letter) => letter,
        _ => return Err("NETWORK_ROOT_DENIED"),
    };
    #[link(name = "kernel32")]
    extern "system" {
        fn GetDriveTypeW(root: *const u16) -> u32;
    }
    let drive_root = [drive as u16, b':' as u16, b'\\' as u16, 0];
    let drive_type = unsafe { GetDriveTypeW(drive_root.as_ptr()) };
    if !matches!(drive_type, 2 | 3) {
        return Err("NETWORK_ROOT_DENIED");
    }
    let file = OpenOptions::new()
        .read(true)
        .share_mode(3)
        .custom_flags(0x0200_0000 | 0x0020_0000)
        .open(path)
        .map_err(|_| "WORKSPACE_ROOT_UNAVAILABLE")?;
    let meta = file.metadata().map_err(|_| "WORKSPACE_ROOT_UNAVAILABLE")?;
    if !meta.is_dir() || meta.file_attributes() & 0x400 != 0 {
        return Err("REPARSE_ROOT_DENIED");
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn detects_only_selected_root_without_reading_project_content() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("selected");
        let project = root.join("demo");
        fs::create_dir_all(project.join(".git")).unwrap();
        fs::write(project.join("Cargo.toml"), b"not parsed or executed").unwrap();
        fs::write(project.join("package.json"), b"not parsed or executed").unwrap();
        fs::write(
            project.join(".git/config"),
            b"[core]\nfsmonitor = malicious-program\n",
        )
        .unwrap();
        fs::create_dir_all(root.join("node_modules/hidden")).unwrap();
        fs::write(root.join("node_modules/hidden/Cargo.toml"), b"ignored").unwrap();
        fs::create_dir_all(root.join("Profiles/private")).unwrap();
        fs::write(root.join("Profiles/private/Cargo.toml"), b"ignored").unwrap();
        assert!(WorkspaceRoot::open_selected(&root.join("Profiles")).is_err());
        let mut ws = WorkspaceRoot::open_selected(&root).unwrap();
        let found = ws.discover().unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].kinds, vec![ProjectKind::Rust, ProjectKind::Web]);
        assert!(found[0].git_detected);
        assert!(found[0].observed_at_ms > 0);
        assert_eq!(found[0].dirty_state, "UNKNOWN_DIRTY_STATE");
        assert_eq!(
            ws.local_relative_path(&found[0].candidate_id, found[0].generation),
            Some(Path::new("demo"))
        );
        assert!(!format!("{found:?}").contains("demo"));
        let next = ws.discover().unwrap();
        assert_eq!(next[0].candidate_id, found[0].candidate_id);
        assert_ne!(next[0].generation, found[0].generation);
        assert!(ws
            .local_relative_path(&found[0].candidate_id, found[0].generation)
            .is_none());
        ws.valid_until = Some(Instant::now());
        assert!(ws
            .local_relative_path(&next[0].candidate_id, next[0].generation)
            .is_none());
    }

    #[test]
    fn refuses_relative_root_and_limits_entry_count() {
        assert!(WorkspaceRoot::open_selected(Path::new("relative")).is_err());
        #[cfg(windows)]
        assert!(WorkspaceRoot::open_selected(Path::new(r"C:\")).is_err());
        #[cfg(unix)]
        assert!(WorkspaceRoot::open_selected(Path::new("/")).is_err());
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("Cargo.toml"), b"").unwrap();
        let mut ws = WorkspaceRoot::open_selected(temp.path()).unwrap();
        let prior = ws.discover().unwrap();
        for i in 0..=MAX_ENTRIES {
            fs::write(temp.path().join(format!("file-{i}")), b"").unwrap();
        }
        assert_eq!(ws.discover(), Err("DISCOVERY_LIMIT"));
        assert!(ws
            .local_relative_path(&prior[0].candidate_id, prior[0].generation)
            .is_none());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_cannot_escape_selected_root() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("selected");
        let outside = temp.path().join("outside");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("Cargo.toml"), b"outside").unwrap();
        std::os::unix::fs::symlink(&outside, root.join("escape")).unwrap();
        let mut ws = WorkspaceRoot::open_selected(&root).unwrap();
        assert!(ws.discover().unwrap().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn case_collisions_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("Cargo.toml"), b"").unwrap();
        fs::write(root.path().join("cargo.toml"), b"").unwrap();
        let mut ws = WorkspaceRoot::open_selected(root.path()).unwrap();
        assert_eq!(ws.discover(), Err("CASE_COLLISION"));
    }

    #[test]
    fn git_head_is_read_without_executing_git_or_config() {
        let root = tempfile::tempdir().unwrap();
        let git = root.path().join(".git");
        fs::create_dir_all(git.join("refs/heads")).unwrap();
        fs::write(git.join("config"), b"[core]\nfsmonitor = malicious\n").unwrap();
        fs::write(git.join("HEAD"), b"ref: refs/heads/main\n").unwrap();
        fs::write(git.join("refs/heads/main"), format!("{}\n", "a".repeat(40))).unwrap();
        let mut ws = WorkspaceRoot::open_selected(root.path()).unwrap();
        let candidate = ws.discover().unwrap().remove(0);
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Observed("a".repeat(40))
        );
        fs::remove_file(git.join("refs/heads/main")).unwrap();
        fs::write(
            git.join("packed-refs"),
            format!("# pack-refs\n{} refs/heads/main\n", "b".repeat(40)),
        )
        .unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Observed("b".repeat(40))
        );
        fs::write(
            git.join("packed-refs"),
            format!(
                "{} refs/heads/main\n{} refs/heads/main\n",
                "a".repeat(40),
                "b".repeat(40)
            ),
        )
        .unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Untrusted
        );
        fs::remove_file(git.join("packed-refs")).unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Unresolved
        );
        fs::write(git.join("HEAD"), format!("{}\n", "c".repeat(64))).unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Observed("c".repeat(64))
        );
        fs::write(git.join("HEAD"), b"ref: refs/heads/../../outside\n").unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Untrusted
        );
        fs::write(git.join("HEAD"), vec![b'a'; 1025]).unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Untrusted
        );
        let next = ws.discover().unwrap().remove(0);
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation),
            Err("STALE_CANDIDATE")
        );
        assert_ne!(candidate.generation, next.generation);
    }

    #[test]
    fn worktree_pointer_and_hardlinked_head_are_not_trusted() {
        let root = tempfile::tempdir().unwrap();
        let project = root.path().join("repo");
        fs::create_dir(&project).unwrap();
        fs::write(project.join(".git"), b"gitdir: ../../outside\n").unwrap();
        let mut ws = WorkspaceRoot::open_selected(root.path()).unwrap();
        let candidate = ws.discover().unwrap().remove(0);
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Unsupported
        );
        fs::remove_file(project.join(".git")).unwrap();
        fs::create_dir(project.join(".git")).unwrap();
        let external = root.path().join("external-head");
        fs::write(&external, format!("{}\n", "a".repeat(40))).unwrap();
        fs::hard_link(&external, project.join(".git/HEAD")).unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Untrusted
        );
    }

    #[cfg(unix)]
    #[test]
    fn probe_uses_discovered_directory_handle_after_path_replacement() {
        let root = tempfile::tempdir().unwrap();
        let project = root.path().join("repo");
        fs::create_dir_all(project.join(".git")).unwrap();
        fs::write(project.join(".git/HEAD"), format!("{}\n", "a".repeat(40))).unwrap();
        let mut ws = WorkspaceRoot::open_selected(root.path()).unwrap();
        let candidate = ws.discover().unwrap().remove(0);
        fs::rename(&project, root.path().join("old")).unwrap();
        fs::create_dir_all(project.join(".git")).unwrap();
        fs::write(project.join(".git/HEAD"), format!("{}\n", "b".repeat(40))).unwrap();
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Observed("a".repeat(40))
        );
    }

    #[cfg(unix)]
    #[test]
    fn git_head_symlink_and_fifo_fail_closed() {
        use std::{ffi::CString, os::unix::ffi::OsStrExt};
        let root = tempfile::tempdir().unwrap();
        let git = root.path().join(".git");
        fs::create_dir(&git).unwrap();
        let external = root.path().join("outside-head");
        fs::write(&external, format!("{}\n", "a".repeat(40))).unwrap();
        std::os::unix::fs::symlink(&external, git.join("HEAD")).unwrap();
        let mut ws = WorkspaceRoot::open_selected(root.path()).unwrap();
        let candidate = ws.discover().unwrap().remove(0);
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Untrusted
        );
        fs::remove_file(git.join("HEAD")).unwrap();
        let path = CString::new(git.join("HEAD").as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
        assert_eq!(
            ws.probe_git(&candidate.candidate_id, candidate.generation)
                .unwrap(),
            GitHead::Untrusted
        );
    }

    #[cfg(windows)]
    #[test]
    fn junction_cannot_escape_selected_root() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("selected");
        let outside = temp.path().join("outside");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("Cargo.toml"), b"outside").unwrap();
        let link = root.join("escape");
        junction::create(&outside, &link).unwrap();
        let mut ws = WorkspaceRoot::open_selected(&root).unwrap();
        let result = ws.discover();
        junction::delete(&link).unwrap();
        assert!(result.is_err() || result.unwrap().is_empty());
    }

    #[cfg(windows)]
    #[test]
    fn git_ref_junction_cannot_escape_project() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("selected");
        let outside = temp.path().join("outside");
        fs::create_dir_all(root.join(".git/refs")).unwrap();
        fs::create_dir(&outside).unwrap();
        fs::write(root.join(".git/HEAD"), b"ref: refs/heads/main\n").unwrap();
        fs::write(outside.join("main"), format!("{}\n", "a".repeat(40))).unwrap();
        let link = root.join(".git/refs/heads");
        junction::create(&outside, &link).unwrap();
        let mut ws = WorkspaceRoot::open_selected(&root).unwrap();
        let candidate = ws.discover().unwrap().remove(0);
        let result = ws
            .probe_git(&candidate.candidate_id, candidate.generation)
            .unwrap();
        junction::delete(&link).unwrap();
        assert_eq!(result, GitHead::Untrusted);
    }
}
