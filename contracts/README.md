# Contracts

The Solidity for DUALITY: an evidence registry that holds the release predicate,
and an ERC-8183 hook that refuses a release when that predicate fails.

## Dependencies

`lib/` is gitignored, so install the dependencies before the first build. Both
are the upstream reference implementations, unmodified:

```bash
forge install erc-8183/base-contracts --no-git
forge install OpenZeppelin/openzeppelin-contracts --no-git
forge build
forge test
```

The remappings in `foundry.toml` point at `lib/base-contracts/contracts/` and at
the OpenZeppelin packages that repository vendors, so
`@openzeppelin/contracts-upgradeable/` resolves inside `lib/base-contracts/lib/`.

## Deploy

```bash
export DEPLOYER_KEY=...        # funded on the target network
export ADMIN_ADDRESS=...       # receives ADMIN_ROLE
export TREASURY_ADDRESS=...
export COMMITTER_ADDRESS=...   # may commit evidence
export QUALIFIER_ADDRESS=...   # may set qualification
forge script script/Deploy.s.sol:Deploy --rpc-url $RPC_URL --broadcast -vv
```

The script whitelists the hook and allowlists the payment token in the same run,
because `createJob` refuses a job whose hook is not whitelisted.
