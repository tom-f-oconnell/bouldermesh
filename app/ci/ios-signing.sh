#!/bin/bash
# What .github/workflows/testflight.yml needs to sign the iPhone app and upload it to TestFlight, made without a Mac
# (openssl) and set as the repository's secrets (gh), in the order app/README.md's "TestFlight" gives:
#
#     app/ci/ios-signing.sh csr                     a key and a certificate signing request, for an Apple Distribution
#                                                   certificate (upload the .csr at developer.apple.com)
#     app/ci/ios-signing.sh p12 <distribution.cer>  that certificate (as Apple sends it) with its key, as a .p12
#     app/ci/ios-signing.sh secrets <AuthKey_XXXXXXXXXX.p8> <issuer id> <team id> <profile.mobileprovision>
#                                                   all of it as the repository's GitHub secrets
#
# Everything stays in ~/.bouldermesh-ios-signing (only you can read it; SIGNING_DIR changes where), and in the
# repository's secrets, which GitHub never shows again. The key in it signs apps as your team: keep it private.
set -euo pipefail
dir=${SIGNING_DIR:-$HOME/.bouldermesh-ios-signing}
mkdir -p "$dir" && chmod 700 "$dir"
umask 077
b64() { base64 -w0 "$1" 2>/dev/null || base64 -i "$1" | tr -d '\n'; }     # GNU's, or a Mac's

case "${1:-}" in
csr)
    [ ! -e "$dir/distribution.key" ] || { echo "$dir/distribution.key is there already: delete it first to make a new one" >&2; exit 1; }
    openssl genrsa -out "$dir/distribution.key" 2048 2>/dev/null
    openssl req -new -key "$dir/distribution.key" -out "$dir/distribution.csr" -subj "/CN=BoulderMesh distribution/C=US"
    echo "made $dir/distribution.csr. At https://developer.apple.com/account/resources/certificates/add choose"
    echo "Apple Distribution, upload that file, download the certificate, then: $0 p12 <the downloaded .cer>"
    ;;
p12)
    cer=${2:?usage: $0 p12 <distribution.cer>}
    [ -e "$dir/distribution.key" ] || { echo "no $dir/distribution.key: run $0 csr first" >&2; exit 1; }
    openssl x509 -inform DER -in "$cer" -out "$dir/distribution.pem" 2>/dev/null || cp "$cer" "$dir/distribution.pem"
    # its key must be the one the request was made with
    [ "$(openssl x509 -in "$dir/distribution.pem" -noout -pubkey)" = "$(openssl rsa -in "$dir/distribution.key" -pubout 2>/dev/null)" ] \
        || { echo "that certificate isn't for $dir/distribution.key" >&2; exit 1; }
    openssl rand -base64 24 | tr -d '\n' > "$dir/p12-password"
    # legacy encryption: macOS's keychain doesn't import OpenSSL 3's default
    openssl pkcs12 -export -inkey "$dir/distribution.key" -in "$dir/distribution.pem" -out "$dir/distribution.p12" \
        -passout "file:$dir/p12-password" -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 2>/dev/null \
        || openssl pkcs12 -export -legacy -inkey "$dir/distribution.key" -in "$dir/distribution.pem" -out "$dir/distribution.p12" \
               -passout "file:$dir/p12-password"
    echo "made $dir/distribution.p12 ($(openssl x509 -in "$dir/distribution.pem" -noout -subject))"
    echo "next: a provisioning profile (App Store Connect, for sh.boulderme.app, with this certificate), then $0 secrets ..."
    ;;
secrets)
    p8=${2:?usage: $0 secrets <AuthKey_XXXXXXXXXX.p8> <issuer id> <team id> <profile.mobileprovision>}
    issuer=${3:?the issuer id of the API key}
    team=${4:?your team id (Membership details)}
    profile=${5:?the App Store provisioning profile}
    key_id=$(basename "$p8" .p8 | sed 's/^AuthKey_//')
    [ -e "$dir/distribution.p12" ] || { echo "no $dir/distribution.p12: run $0 csr and $0 p12 first" >&2; exit 1; }
    command -v gh > /dev/null || { echo "needs gh (GitHub's CLI), signed in with access to the repository" >&2; exit 1; }
    cp "$p8" "$dir/" && cp "$profile" "$dir/appstore.mobileprovision"
    gh secret set APPLE_TEAM_ID --body "$team"
    gh secret set ASC_KEY_ID --body "$key_id"
    gh secret set ASC_ISSUER_ID --body "$issuer"
    gh secret set ASC_KEY_P8 < "$p8"
    b64 "$dir/distribution.p12" | gh secret set IOS_DIST_P12
    gh secret set IOS_DIST_P12_PASSWORD < "$dir/p12-password"
    b64 "$profile" | gh secret set IOS_APPSTORE_PROFILE
    echo "set the seven secrets. gh workflow run testflight.yml uploads a build now; every push that changes app/ does too"
    ;;
*)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
