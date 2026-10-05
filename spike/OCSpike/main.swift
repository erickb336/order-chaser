// R10 spike: can a signed helper hold a Touch ID-protected Keychain item that other programs cannot read?
// SPIKE ONLY. The OWNER runs it through owner-test.sh. It never prints a stored value.
import Foundation
import LocalAuthentication
import Security

let account = "spike"

func baseQuery(_ service: String) -> [String: Any] {
    [kSecClass as String: kSecClassGenericPassword,
     kSecAttrService as String: service,
     kSecAttrAccount as String: account,
     kSecUseDataProtectionKeychain as String: true]
}

/// Reads the value from stdin. On a terminal, echo is off while the owner types.
func readSecret() -> Data {
    var data: Data
    if isatty(STDIN_FILENO) == 1 {
        var old = termios(); tcgetattr(STDIN_FILENO, &old)
        var quiet = old; quiet.c_lflag &= ~tcflag_t(ECHO)
        tcsetattr(STDIN_FILENO, TCSANOW, &quiet)
        FileHandle.standardError.write(Data("value (not shown): ".utf8))
        data = Data((readLine(strippingNewline: true) ?? "").utf8)
        tcsetattr(STDIN_FILENO, TCSANOW, &old)
        FileHandle.standardError.write(Data("\n".utf8))
    } else {
        data = FileHandle.standardInput.readDataToEndOfFile()
        while data.last == 0x0A || data.last == 0x0D { data.removeLast() }
    }
    return data
}

func store(_ service: String) -> OSStatus {
    let secret = readSecret()
    guard !secret.isEmpty else { return errSecParam }
    var error: Unmanaged<CFError>?
    guard let access = SecAccessControlCreateWithFlags(
        nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, .biometryCurrentSet, &error)
    else { return errSecParam }
    var q = baseQuery(service)
    q[kSecValueData as String] = secret
    q[kSecAttrAccessControl as String] = access
    return SecItemAdd(q as CFDictionary, nil)
}

/// A fresh context for each read, with no reuse, so each read needs its own touch.
func read(_ service: String, orderText: String) -> (OSStatus, Int) {
    let context = LAContext()
    context.touchIDAuthenticationAllowableReuseDuration = 0
    context.localizedReason = orderText  // macOS shows: "OCSpike is trying to <order text>."
    var q = baseQuery(service)
    q[kSecReturnData as String] = true
    q[kSecUseAuthenticationContext as String] = context
    var out: CFTypeRef?
    let status = SecItemCopyMatching(q as CFDictionary, &out)
    context.invalidate()
    return (status, (out as? Data)?.count ?? 0)
}

func whoami() -> OSStatus {
    var code: SecCode?
    var status = SecCodeCopySelf([], &code)
    guard status == errSecSuccess, let code else { return status }
    var staticCode: SecStaticCode?
    status = SecCodeCopyStaticCode(code, [], &staticCode)
    guard status == errSecSuccess, let staticCode else { return status }
    var info: CFDictionary?
    status = SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &info)
    guard status == errSecSuccess, let dict = info as? [String: Any] else { return status }
    print("team:", dict[kSecCodeInfoTeamIdentifier as String] as? String ?? "(none: ad-hoc or unsigned)")
    print("identifier:", dict[kSecCodeInfoIdentifier as String] as? String ?? "(none)")
    let entitlements = dict[kSecCodeInfoEntitlementsDict as String] as? [String: Any] ?? [:]
    print("entitlements:", entitlements.isEmpty ? "(none)" : "")
    for key in entitlements.keys.sorted() { print("  \(key) = \(entitlements[key]!)") }
    return errSecSuccess
}

func finish(_ name: String, _ status: OSStatus, _ extra: String = "") -> Never {
    print("\(name): status=\(status)\(extra)")
    exit(status == errSecSuccess ? 0 : 1)
}

let args = CommandLine.arguments
switch (args.count > 1 ? args[1] : "", args.count) {
case ("store", 3): finish("store", store(args[2]))
case ("read", 4): let (s, n) = read(args[2], orderText: args[3]); finish("read", s, " bytes=\(n)")
case ("delete", 3): finish("delete", SecItemDelete(baseQuery(args[2]) as CFDictionary))
case ("whoami", 2): let s = whoami(); exit(s == errSecSuccess ? 0 : 1)
default:
    FileHandle.standardError.write(Data("""
    usage: oc-spike store <service>               (value from stdin, not echoed)
           oc-spike read <service> <order-text>   (prints only the byte count)
           oc-spike delete <service>
           oc-spike whoami                        (signing team and entitlements)

    """.utf8))
    exit(2)
}
