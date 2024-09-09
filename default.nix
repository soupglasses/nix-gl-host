{ pkgs ? import <nixpkgs> { }, lib ? pkgs.lib }:

pkgs.stdenvNoCC.mkDerivation {
  pname = "nix-gl-host";
  version = "0.1";
  src = lib.cleanSource ./.;
  nativeBuildInputs = [ pkgs.python3 ];

  installPhase = ''
    install -D -m0755 src/nixglhost.py $out/bin/nixglhost
  '';

  postFixup = ''
    substituteInPlace $out/bin/nixglhost \
        --replace "@patchelf-bin@" "${pkgs.patchelf}/bin/patchelf" \
        --replace "IN_NIX_STORE = False" "IN_NIX_STORE = True"
    patchShebangs $out/bin/nixglhost
  '';

  doCheck = true;

  checkPhase = ''
    python src/nixglhost_test.py
  '';

  meta.mainProgram = "nixglhost";
}
