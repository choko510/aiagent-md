use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

const ENC_PREFIX: &str = "enc:v1:";
const KEY_ENTROPY: &[u8] = b"aiagent-device-journal-key-v1";

pub fn random_bytes<const N: usize>() -> Result<[u8; N], String> {
    let mut buf = [0u8; N];
    fill_random(&mut buf)?;
    Ok(buf)
}

#[cfg(windows)]
fn fill_random(buf: &mut [u8]) -> Result<(), String> {
    #[link(name = "bcrypt")]
    extern "system" {
        fn BCryptGenRandom(
            h_algorithm: *mut core::ffi::c_void,
            pb_buffer: *mut u8,
            cb_buffer: u32,
            dw_flags: u32,
        ) -> i32;
    }
    const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 0x0000_0002;
    let len = u32::try_from(buf.len()).map_err(|_| "random buffer too large")?;
    let status = unsafe {
        BCryptGenRandom(
            core::ptr::null_mut(),
            buf.as_mut_ptr(),
            len,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        return Err(format!("BCryptGenRandom failed: 0x{status:08x}"));
    }
    Ok(())
}

#[cfg(unix)]
fn fill_random(buf: &mut [u8]) -> Result<(), String> {
    use std::io::Read;
    let mut f = fs::File::open("/dev/urandom").map_err(|e| e.to_string())?;
    f.read_exact(buf).map_err(|e| e.to_string())
}

pub fn restrict_path_owner_only(path: &Path, is_dir: bool) -> Result<(), String> {
    if !path.exists() {
        return Ok(());
    }
    restrict_path_owner_only_impl(path, is_dir)
}

#[cfg(unix)]
fn restrict_path_owner_only_impl(path: &Path, is_dir: bool) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let mode = if is_dir { 0o700 } else { 0o600 };
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).map_err(|e| e.to_string())
}

#[cfg(windows)]
fn restrict_path_owner_only_impl(path: &Path, is_dir: bool) -> Result<(), String> {
    windows_acl::restrict_owner_only(path, is_dir)
}

#[cfg(windows)]
pub fn verify_owner_only_acl(path: &Path) -> Result<bool, String> {
    windows_acl::verify_owner_only(path)
}

#[cfg(unix)]
pub fn verify_owner_only_acl(path: &Path) -> Result<bool, String> {
    use std::os::unix::fs::PermissionsExt;
    let mode = fs::metadata(path)
        .map_err(|e| e.to_string())?
        .permissions()
        .mode()
        & 0o777;
    Ok(mode & 0o077 == 0)
}

pub fn key_file_path(db_path: &Path) -> PathBuf {
    let mut os = db_path.as_os_str().to_os_string();
    os.push(".key");
    PathBuf::from(os)
}

pub fn load_or_create_journal_key(db_path: &Path) -> Result<[u8; 32], String> {
    let key_path = key_file_path(db_path);
    if key_path.exists() {
        restrict_path_owner_only(&key_path, false)?;
        let raw = fs::read(&key_path).map_err(|e| e.to_string())?;
        return unprotect_key_blob(&raw);
    }
    let key: [u8; 32] = random_bytes()?;
    let protected = protect_key_blob(&key)?;
    match write_new_restricted_file(&key_path, &protected) {
        Ok(()) => {
            restrict_path_owner_only(&key_path, false)?;
            Ok(key)
        }
        Err(e) if key_path.exists() => {
            let _ = e;
            restrict_path_owner_only(&key_path, false)?;
            let raw = fs::read(&key_path).map_err(|err| err.to_string())?;
            unprotect_key_blob(&raw)
        }
        Err(e) => Err(e),
    }
}

fn write_new_restricted_file(path: &Path, data: &[u8]) -> Result<(), String> {
    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut file = opts.open(path).map_err(|e| e.to_string())?;
    file.write_all(data).map_err(|e| e.to_string())?;
    file.sync_all().map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(windows)]
fn protect_key_blob(key: &[u8; 32]) -> Result<Vec<u8>, String> {
    windows_dpapi::protect(key, KEY_ENTROPY)
}

#[cfg(windows)]
fn unprotect_key_blob(blob: &[u8]) -> Result<[u8; 32], String> {
    let raw = windows_dpapi::unprotect(blob, KEY_ENTROPY)?;
    <[u8; 32]>::try_from(raw.as_slice()).map_err(|_| "corrupt journal key length".into())
}

#[cfg(unix)]
fn protect_key_blob(key: &[u8; 32]) -> Result<Vec<u8>, String> {
    Ok(key.to_vec())
}

#[cfg(unix)]
fn unprotect_key_blob(blob: &[u8]) -> Result<[u8; 32], String> {
    <[u8; 32]>::try_from(blob).map_err(|_| "corrupt journal key length".into())
}

fn derive_subkey(master: &[u8; 32], domain: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(master);
    hasher.update(domain);
    hasher.finalize().into()
}

fn hmac_sha256(key: &[u8; 32], msg: &[u8]) -> [u8; 32] {
    let mut ipad = [0x36u8; 64];
    let mut opad = [0x5cu8; 64];
    for (i, &b) in key.iter().enumerate() {
        ipad[i] ^= b;
        opad[i] ^= b;
    }
    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(msg);
    let inner_hash = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_hash);
    outer.finalize().into()
}

fn ctr_xor(enc_key: &[u8; 32], nonce: &[u8; 16], data: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    let mut counter: u32 = 0;
    for chunk in data.chunks(32) {
        let mut h = Sha256::new();
        h.update(enc_key);
        h.update(nonce);
        h.update(counter.to_be_bytes());
        let block = h.finalize();
        for (&b, &k) in chunk.iter().zip(block.iter()) {
            out.push(b ^ k);
        }
        counter = counter.wrapping_add(1);
    }
    out
}

fn build_aad(scope_hash: &str, label: &str, nonce: &[u8; 16], ciphertext: &[u8]) -> Vec<u8> {
    let mut msg =
        Vec::with_capacity(32 + scope_hash.len() + label.len() + nonce.len() + ciphertext.len());
    msg.extend_from_slice(b"aiagent-journal-v1|");
    msg.extend_from_slice(scope_hash.as_bytes());
    msg.push(b'|');
    msg.extend_from_slice(label.as_bytes());
    msg.push(b'|');
    msg.extend_from_slice(nonce);
    msg.extend_from_slice(ciphertext);
    msg
}

pub fn seal_payload(
    master_key: &[u8; 32],
    scope_hash: &str,
    label: &str,
    plaintext: &[u8],
) -> Result<String, String> {
    let enc_key = derive_subkey(master_key, b"|enc-v1");
    let mac_key = derive_subkey(master_key, b"|mac-v1");
    let nonce: [u8; 16] = random_bytes()?;
    let ciphertext = ctr_xor(&enc_key, &nonce, plaintext);
    let aad = build_aad(scope_hash, label, &nonce, &ciphertext);
    let tag = hmac_sha256(&mac_key, &aad);
    Ok(format!(
        "{ENC_PREFIX}{}:{}:{}",
        encode_hex(&nonce),
        encode_hex(&tag),
        encode_hex(&ciphertext)
    ))
}

pub fn open_payload(
    master_key: &[u8; 32],
    scope_hash: &str,
    label: &str,
    encoded: &str,
) -> Result<Vec<u8>, String> {
    let rest = encoded
        .strip_prefix(ENC_PREFIX)
        .ok_or_else(|| "unencrypted or unsupported journal payload".to_string())?;
    let mut parts = rest.splitn(3, ':');
    let nonce_hex = parts.next().ok_or("missing journal nonce")?;
    let tag_hex = parts.next().ok_or("missing journal mac tag")?;
    let ct_hex = parts.next().ok_or("missing journal ciphertext")?;
    let nonce_vec = decode_hex(nonce_hex)?;
    let tag_vec = decode_hex(tag_hex)?;
    let ciphertext = decode_hex(ct_hex)?;
    let nonce: [u8; 16] = nonce_vec
        .try_into()
        .map_err(|_| "invalid journal nonce length")?;
    let expected_tag: [u8; 32] = tag_vec
        .try_into()
        .map_err(|_| "invalid journal mac tag length")?;

    let enc_key = derive_subkey(master_key, b"|enc-v1");
    let mac_key = derive_subkey(master_key, b"|mac-v1");
    let aad = build_aad(scope_hash, label, &nonce, &ciphertext);
    let actual_tag = hmac_sha256(&mac_key, &aad);
    let diff = actual_tag
        .iter()
        .zip(expected_tag.iter())
        .fold(0u8, |acc, (a, b)| acc | (a ^ b));
    if diff != 0 {
        return Err("journal integrity check failed".into());
    }
    Ok(ctr_xor(&enc_key, &nonce, &ciphertext))
}

fn encode_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

fn decode_hex(input: &str) -> Result<Vec<u8>, String> {
    if !input.len().is_multiple_of(2) {
        return Err("odd hex length".into());
    }
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len() / 2);
    for i in (0..bytes.len()).step_by(2) {
        let hi = hex_nibble(bytes[i])?;
        let lo = hex_nibble(bytes[i + 1])?;
        out.push((hi << 4) | lo);
    }
    Ok(out)
}

fn hex_nibble(b: u8) -> Result<u8, String> {
    match b {
        b'0'..=b'9' => Ok(b - b'0'),
        b'a'..=b'f' => Ok(b - b'a' + 10),
        b'A'..=b'F' => Ok(b - b'A' + 10),
        _ => Err("invalid hex character".into()),
    }
}

#[cfg(windows)]
mod windows_dpapi {
    use core::ffi::c_void;

    #[repr(C)]
    struct CryptDataBlob {
        cb_data: u32,
        pb_data: *mut u8,
    }

    #[link(name = "crypt32")]
    extern "system" {
        fn CryptProtectData(
            p_data_in: *const CryptDataBlob,
            sz_data_descr: *const u16,
            p_optional_entropy: *const CryptDataBlob,
            pv_reserved: *mut c_void,
            p_prompt_struct: *mut c_void,
            dw_flags: u32,
            p_data_out: *mut CryptDataBlob,
        ) -> i32;
        fn CryptUnprotectData(
            p_data_in: *const CryptDataBlob,
            ppsz_data_descr: *mut *mut u16,
            p_optional_entropy: *const CryptDataBlob,
            pv_reserved: *mut c_void,
            p_prompt_struct: *mut c_void,
            dw_flags: u32,
            p_data_out: *mut CryptDataBlob,
        ) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn LocalFree(h_mem: *mut c_void) -> *mut c_void;
    }

    const CRYPTPROTECT_UI_FORBIDDEN: u32 = 0x1;

    pub fn protect(data: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
        let in_blob = CryptDataBlob {
            cb_data: u32::try_from(data.len()).map_err(|_| "data too large")?,
            pb_data: data.as_ptr().cast_mut(),
        };
        let ent_blob = CryptDataBlob {
            cb_data: u32::try_from(entropy.len()).map_err(|_| "entropy too large")?,
            pb_data: entropy.as_ptr().cast_mut(),
        };
        let mut out_blob = CryptDataBlob {
            cb_data: 0,
            pb_data: core::ptr::null_mut(),
        };
        let ok = unsafe {
            CryptProtectData(
                &in_blob,
                core::ptr::null(),
                &ent_blob,
                core::ptr::null_mut(),
                core::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out_blob,
            )
        };
        if ok == 0 {
            return Err(format!(
                "CryptProtectData failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        let slice =
            unsafe { core::slice::from_raw_parts(out_blob.pb_data, out_blob.cb_data as usize) };
        let result = slice.to_vec();
        unsafe {
            LocalFree(out_blob.pb_data.cast());
        }
        Ok(result)
    }

    pub fn unprotect(blob: &[u8], entropy: &[u8]) -> Result<Vec<u8>, String> {
        let in_blob = CryptDataBlob {
            cb_data: u32::try_from(blob.len()).map_err(|_| "blob too large")?,
            pb_data: blob.as_ptr().cast_mut(),
        };
        let ent_blob = CryptDataBlob {
            cb_data: u32::try_from(entropy.len()).map_err(|_| "entropy too large")?,
            pb_data: entropy.as_ptr().cast_mut(),
        };
        let mut out_blob = CryptDataBlob {
            cb_data: 0,
            pb_data: core::ptr::null_mut(),
        };
        let ok = unsafe {
            CryptUnprotectData(
                &in_blob,
                core::ptr::null_mut(),
                &ent_blob,
                core::ptr::null_mut(),
                core::ptr::null_mut(),
                CRYPTPROTECT_UI_FORBIDDEN,
                &mut out_blob,
            )
        };
        if ok == 0 {
            return Err(format!(
                "CryptUnprotectData failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        let slice =
            unsafe { core::slice::from_raw_parts(out_blob.pb_data, out_blob.cb_data as usize) };
        let result = slice.to_vec();
        unsafe {
            LocalFree(out_blob.pb_data.cast());
        }
        Ok(result)
    }
}

#[cfg(windows)]
mod windows_acl {
    use core::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;
    use std::path::Path;

    #[repr(C)]
    struct TokenUser {
        sid: *mut c_void,
        _attributes: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetCurrentProcess() -> *mut c_void;
        fn CloseHandle(h_object: *mut c_void) -> i32;
        fn LocalFree(h_mem: *mut c_void) -> *mut c_void;
    }

    #[link(name = "advapi32")]
    extern "system" {
        fn OpenProcessToken(
            process_handle: *mut c_void,
            desired_access: u32,
            token_handle: *mut *mut c_void,
        ) -> i32;
        fn GetTokenInformation(
            token_handle: *mut c_void,
            token_information_class: u32,
            token_information: *mut u8,
            token_information_length: u32,
            return_length: *mut u32,
        ) -> i32;
        fn ConvertSidToStringSidW(sid: *const c_void, string_sid: *mut *mut u16) -> i32;
        fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
            string_security_descriptor: *const u16,
            string_sd_revision: u32,
            security_descriptor: *mut *mut c_void,
            security_descriptor_size: *mut u32,
        ) -> i32;
        fn GetSecurityDescriptorDacl(
            p_security_descriptor: *mut c_void,
            lpb_dacl_present: *mut i32,
            p_dacl: *mut *mut c_void,
            lpb_dacl_defaulted: *mut i32,
        ) -> i32;
        fn SetNamedSecurityInfoW(
            p_object_name: *const u16,
            object_type: u32,
            security_info: u32,
            psid_owner: *mut c_void,
            psid_group: *mut c_void,
            p_dacl: *mut c_void,
            p_sacl: *mut c_void,
        ) -> u32;
        fn ConvertSecurityDescriptorToStringSecurityDescriptorW(
            security_descriptor: *mut c_void,
            requested_string_sd_revision: u32,
            security_information: u32,
            string_security_descriptor: *mut *mut u16,
            string_security_descriptor_len: *mut u32,
        ) -> i32;
        fn GetNamedSecurityInfoW(
            p_object_name: *const u16,
            object_type: u32,
            security_info: u32,
            ppsid_owner: *mut *mut c_void,
            ppsid_group: *mut *mut c_void,
            pp_dacl: *mut *mut c_void,
            pp_sacl: *mut *mut c_void,
            pp_security_descriptor: *mut *mut c_void,
        ) -> u32;
    }

    const TOKEN_QUERY: u32 = 0x0008;
    const TOKEN_USER: u32 = 1;
    const SDDL_REVISION_1: u32 = 1;
    const SE_FILE_OBJECT: u32 = 1;
    const DACL_SECURITY_INFORMATION: u32 = 0x0000_0004;
    const PROTECTED_DACL_SECURITY_INFORMATION: u32 = 0x8000_0000;

    fn wide(s: &std::ffi::OsStr) -> Vec<u16> {
        s.encode_wide().chain(core::iter::once(0)).collect()
    }

    fn wide_str(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(core::iter::once(0)).collect()
    }

    fn wide_ptr_to_string(ptr: *const u16) -> String {
        let mut len = 0usize;
        while unsafe { *ptr.add(len) } != 0 {
            len += 1;
        }
        let slice = unsafe { core::slice::from_raw_parts(ptr, len) };
        String::from_utf16_lossy(slice)
    }

    fn current_user_sid_string() -> Result<String, String> {
        let mut token: *mut c_void = core::ptr::null_mut();
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
            return Err(format!(
                "OpenProcessToken failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        struct TokenGuard(*mut c_void);
        impl Drop for TokenGuard {
            fn drop(&mut self) {
                unsafe {
                    CloseHandle(self.0);
                }
            }
        }
        let _guard = TokenGuard(token);

        let mut needed: u32 = 0;
        unsafe {
            GetTokenInformation(token, TOKEN_USER, core::ptr::null_mut(), 0, &mut needed);
        }
        if needed == 0 {
            return Err("GetTokenInformation size failed".into());
        }
        let mut buf = vec![0u8; needed as usize];
        if unsafe { GetTokenInformation(token, TOKEN_USER, buf.as_mut_ptr(), needed, &mut needed) }
            == 0
        {
            return Err(format!(
                "GetTokenInformation failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        let user = unsafe { &*(buf.as_ptr().cast::<TokenUser>()) };
        let mut sid_w: *mut u16 = core::ptr::null_mut();
        if unsafe { ConvertSidToStringSidW(user.sid, &mut sid_w) } == 0 {
            return Err(format!(
                "ConvertSidToStringSidW failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        let sid = wide_ptr_to_string(sid_w);
        unsafe {
            LocalFree(sid_w.cast());
        }
        Ok(sid)
    }

    pub fn restrict_owner_only(path: &Path, is_dir: bool) -> Result<(), String> {
        let sid = current_user_sid_string()?;
        let sddl = if is_dir {
            format!("D:P(A;OICI;FA;;;{sid})")
        } else {
            format!("D:P(A;;FA;;;{sid})")
        };
        let sddl_w = wide_str(&sddl);
        let mut sd: *mut c_void = core::ptr::null_mut();
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl_w.as_ptr(),
                SDDL_REVISION_1,
                &mut sd,
                core::ptr::null_mut(),
            )
        } == 0
        {
            return Err(format!(
                "ConvertStringSecurityDescriptor failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        struct LocalGuard(*mut c_void);
        impl Drop for LocalGuard {
            fn drop(&mut self) {
                unsafe {
                    LocalFree(self.0);
                }
            }
        }
        let _sd_guard = LocalGuard(sd);

        let mut dacl_present: i32 = 0;
        let mut dacl: *mut c_void = core::ptr::null_mut();
        let mut dacl_defaulted: i32 = 0;
        if unsafe {
            GetSecurityDescriptorDacl(sd, &mut dacl_present, &mut dacl, &mut dacl_defaulted)
        } == 0
            || dacl_present == 0
        {
            return Err("GetSecurityDescriptorDacl failed".into());
        }
        let path_w = wide(path.as_os_str());
        let rc = unsafe {
            SetNamedSecurityInfoW(
                path_w.as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                core::ptr::null_mut(),
                core::ptr::null_mut(),
                dacl,
                core::ptr::null_mut(),
            )
        };
        if rc != 0 {
            return Err(format!("SetNamedSecurityInfoW failed: error {rc}"));
        }
        Ok(())
    }

    pub fn verify_owner_only(path: &Path) -> Result<bool, String> {
        let sid = current_user_sid_string()?;
        let path_w = wide(path.as_os_str());
        let mut sd: *mut c_void = core::ptr::null_mut();
        let rc = unsafe {
            GetNamedSecurityInfoW(
                path_w.as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                core::ptr::null_mut(),
                core::ptr::null_mut(),
                core::ptr::null_mut(),
                core::ptr::null_mut(),
                &mut sd,
            )
        };
        if rc != 0 {
            return Err(format!("GetNamedSecurityInfoW failed: error {rc}"));
        }
        let mut sddl_w: *mut u16 = core::ptr::null_mut();
        let ok = unsafe {
            ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd,
                SDDL_REVISION_1,
                DACL_SECURITY_INFORMATION,
                &mut sddl_w,
                core::ptr::null_mut(),
            )
        };
        unsafe {
            LocalFree(sd);
        }
        if ok == 0 {
            return Err("ConvertSecurityDescriptorToStringSecurityDescriptorW failed".into());
        }
        let sddl = wide_ptr_to_string(sddl_w);
        unsafe {
            LocalFree(sddl_w.cast());
        }
        let header = sddl.split('(').next().unwrap_or("");
        let has_single_owner_ace = header.starts_with("D:P")
            && !sddl.contains(";ID;")
            && sddl.matches('(').count() == 1
            && sddl.contains(&format!(";;;{sid})"));
        Ok(has_single_owner_ace)
    }
}
