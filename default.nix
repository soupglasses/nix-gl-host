{ pkgs ? import <nixpkgs> { }, lib ? pkgs.lib }:

pkgs.python3Packages.buildPythonPackage {
  pname = "nixglhost";
  version = "0.1.0";
  pyproject = true;
  build-system = [ pkgs.hatch ];

  src = lib.cleanSource ./.;

  postPatch = ''
    substituteInPlace nixglhost/main.py \
        --replace-fail "@patchelf-bin@" "${pkgs.patchelf}/bin/patchelf" \
        --replace-fail "IN_NIX_STORE = False" "IN_NIX_STORE = True"
  '';

  doCheck = true;

  checkPhase = ''
    python nixglhost_test.py
  '';

  meta = {
    mainProgram = "nixglhost";
    description = "Run OpenGL/Cuda programs built with Nix, on all Linux distributions";
    homepage = "https://github.com/numtide/nix-gl-host";
    license = lib.licenses.asl20;
    maintainers = with lib.maintainers; [ picnoir soupglasses ];
  };
}
