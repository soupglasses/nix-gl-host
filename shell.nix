{ pkgs ? import <nixpkgs> { } }:

pkgs.mkShellNoCC {
  shellHook = ''
    ${pkgs.pre-commit}/bin/pre-commit install --install-hooks --overwrite
  '';
  nativeBuildInputs = with pkgs; [
    nixpkgs-fmt
    editorconfig-checker
    python3Packages.ipython
    python3Packages.black
    python3Packages.mypy
  ];
}
