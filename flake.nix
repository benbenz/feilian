{
  description = "Node.js 22 development shell for crawler";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;          
        };

        isDarwinArm = system == "aarch64-darwin";

        ollamaPkg = if isDarwinArm then null else
          pkgs.ollama.override {
            acceleration = "cuda";
          };

        buildInputs = [
          pkgs.python312
          pkgs.stdenv.cc.cc.lib
          pkgs.zlib
          pkgs.just
        ]
        ++ (if isDarwinArm then [] else [
          pkgs.xvfb-run
          pkgs.xorg.xorgserver
          pkgs.xorg.xauth ollamaPkg
        ]);
      in {
        devShells.default = pkgs.mkShell {
          inherit buildInputs;
          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib;${pkgs.zlib}/lib";
          '';
        };
      }
    );
} 