# Derivation for nixpkgs. Destination: pkgs/by-name/hb/hbkit/package.nix
{
  lib,
  python3Packages,
  fetchPypi,
}:

python3Packages.buildPythonApplication rec {
  pname = "hbkit";
  version = "0.4.2";
  pyproject = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
  };

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    textual
    pynacl
    cryptography
  ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  pythonImportsCheck = [ "hbkit" ];

  meta = {
    description = "Recover files from Synology Hyper Backup (.hbk) archives";
    longDescription = ''
      hbkit reads Synology Hyper Backup archives directly, without a NAS or any
      Synology software. It restores the original directory tree and mtimes,
      verifies every chunk against the checksums stored in the archive, and can
      read client-side encrypted archives given the password. Includes a
      full-screen browser (hbk-tui) and can work against an archive kept in S3 or
      R2 over an rclone mount.
    '';
    homepage = "https://github.com/YordiLorenzo/hbkit";
    changelog = "https://github.com/YordiLorenzo/hbkit/releases/tag/v${version}";
    license = lib.licenses.mit;
    mainProgram = "hbk";
    maintainers = with lib.maintainers; [ ];
  };
}
