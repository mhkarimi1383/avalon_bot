import enum
import pickle
import re
from datetime import datetime
from random import sample
from typing import Generic, TypeVar, Optional

import aioredis

from avalon import config, exceptions
from avalon.config import REDIS_PREFIX_GAME
from avalon.exceptions import InvalidActionException, OnlyKingCanDo, OnlyLadyCanDo, InvalidParticipant, \
    OnlyAssassinCanDo

redis_client = aioredis.from_url(config.REDIS_URL)
SUCCESS_EMOJI = "🏆"
FAIL_EMOJI = "🏴‍☠️"
KING_EMOJI = "👑"
LADY_EMOJI = "👱‍♀️"


def verify_identity(identity):
    if not isinstance(identity, str) or not re.match(r'^[\w-]{0,64}\Z', identity):
        raise ValueError('Invalid identity: ' + str(identity))


class Participant:
    def __init__(self, identity: str):
        verify_identity(identity)
        self.identity = identity
        self.role: Optional[Role] = None
        self.vote: Optional[bool] = None
        self.quest_action: Optional[bool] = None

    @property
    def current_vote_text(self):
        if self.vote is None:
            return 'Not voted'
        return 'Approved' if self.vote else 'Rejected'

    @property
    def current_quest_action_text(self):
        if self.vote is None:
            return 'Nothing'
        return 'Success' if self.quest_action else 'Fail'

    def __eq__(self, other):
        return isinstance(other, Participant) and self.identity == other.identity

    def __str__(self):
        return self.identity


T = TypeVar('T', bound=Participant)


class GamePhase(enum.Enum):
    Joining = 'Joining'
    Started = 'Started'
    TeamBuilding = 'TeamBuilding'
    TeamVote = 'TeamVote'
    Quest = 'Quest'
    Lady = 'Lady'
    GuessMerlin = 'GuessMerlin'
    Finished = 'Finished'


class Role(enum.Enum):
    Merlin = 'Merlin'
    Percival = 'Percival'
    Servant = 'Servant'
    Mordred = 'Mordred'
    Assassin = 'Assassin'
    Morgana = 'Morgana'
    Minion = 'Minion'
    Oberon = 'Oberon'

    @property
    def is_evil(self):
        return self not in SERVANT_ROLES

    @property
    def emoji(self):
        return ROLE_EMOJI[self]


SERVANT_ROLES = [Role.Merlin, Role.Servant, Role.Percival]
MERLIN_INFO = [Role.Minion, Role.Morgana, Role.Assassin]
PERCIVAL_INFO = [Role.Merlin, Role.Morgana]
EVIL_INFO = [Role.Minion, Role.Morgana, Role.Assassin, Role.Mordred]
ROLE_EMOJI = {
    Role.Merlin: '🎅🏻',
    Role.Percival: '🏇',
    Role.Servant: '🤵',
    Role.Mordred: '🎩',
    Role.Assassin: '☠️',
    Role.Morgana: '🦹‍♀️',
    Role.Minion: '💀',
    Role.Oberon: '👹',
}


class GamePlan:
    def __init__(self, steps, roles, lady_step=2):
        self.roles = [Role[r] for r in roles.split(',')]
        # noinspection PyTypeChecker
        self.steps: list[tuple[int, int]] = [list(map(int, st.split('/'))) for st in steps.split()]
        assert len(self.steps) == 5, steps
        self.lady_step = lady_step
        assert Role.Percival not in self.roles or (Role.Merlin in self.roles and Role.Morgana in self.roles)


GAME_PLANS = {
    # 5: GamePlan('1/2,1/3,1/2,1/3,1/3', 'Servant,Percival,Merlin,Assassin,Morgana'),
    5: GamePlan('1/2 1/3 1/2 1/3 1/3', 'Servant,Servant,Merlin,Assassin,Mordred'),
    6: GamePlan('1/2 1/3 1/4 1/3 1/4', 'Servant,Servant,Percival,Merlin,Assassin,Morgana'),
    7: GamePlan('1/2 1/3 1/3 2/4 1/4', 'Servant,Servant,Servant,Merlin,Assassin,Minion,Minion'),
    8: GamePlan('1/3 1/4 1/4 2/5 1/5', 'Servant,Servant,Servant,Percival,Merlin,Assassin,Morgana,Mordred'),
    9: GamePlan('1/3 1/4 1/4 2/5 1/5', 'Servant,Servant,Servant,Servant,Percival,Merlin,Assassin,Morgana,Mordred'),
    10: GamePlan('1/3 1/4 1/4 2/5 1/5',
                 'Servant,Servant,Servant,Servant,Percival,Merlin,Assassin,Morgana,Minion,Oberon'),
}
if config.GAME_DEBUG:
    GAME_PLANS[2]: GamePlan('1/1 1/1 1/1 1/1 1/1', 'Merlin,Assassin')


class Game(Generic[T]):
    def __init__(self, game_id: str):
        verify_identity(game_id)
        self.created = self.last_save = datetime.utcnow()
        self.game_result: Optional[bool] = None  # True: servant-won, False: evil-won
        self.failed_voting_count = 0
        self.game_id = game_id
        self.participants: list[T] = []
        self.current_team: list[T] = []
        self.phase = GamePhase.Joining
        self.round_result: list[bool] = []  # True: servant-won, False: evil-won
        self.king: Optional[T] = None
        self.lady: Optional[T] = None
        self.past_ladies: list[T] = []

    def add_participant(self, participant: T):
        self.require_game_phase(GamePhase.Joining)
        try:
            self.get_participant_by_id(participant.identity)
            raise exceptions.AlreadyJoined
        except InvalidParticipant:
            self.participants.append(participant)

    def remove_participant(self, participant: T):
        self.require_game_phase(GamePhase.Joining)
        try:
            self.get_participant_by_id(participant.identity)
        except InvalidParticipant:
            raise exceptions.NotJoined from None
        self.participants = [p for p in self.participants if p.identity != participant.identity]

    @property
    def plan(self) -> GamePlan:
        return GAME_PLANS[len(self.participants)]

    @property
    def step(self) -> tuple[int, int]:
        return self.plan.steps[len(self.round_result)]

    def play(self):
        self.require_game_phase(GamePhase.Joining)
        if len(self.participants) not in GAME_PLANS:
            raise InvalidActionException('Game should have 5 to 10 participants')
        for role, p in zip(sample(self.plan.roles, len(self.participants)), self.participants):
            p.role = role
        self.king, self.lady = sample(self.participants, 2)
        self.phase = GamePhase.Started

    def get_user_info(self, pr: T):
        msg = f'You role: {pr.role.value}'
        if pr.role == Role.Merlin:
            msg += ', Evil: {}'.format(', '.join(str(p) for p in self.participants if p.role in MERLIN_INFO))
        if pr.role == Role.Percival:
            msg += ', Morgana/Merlin: {}'.format(
                ', '.join(str(p) for p in self.participants if p.role in PERCIVAL_INFO))
        if pr.role.is_evil:
            msg += ', Teammates: {}'.format(
                ', '.join(str(p) for p in self.participants if p.role in EVIL_INFO and pr != p))
        return msg

    def proceed_to_game(self):
        self.require_game_phase(GamePhase.Started)
        self.phase = GamePhase.TeamBuilding

    def select_for_team(self, participant: T, identity: str):
        self.require_game_phase(GamePhase.TeamBuilding)
        if self.king != participant:
            raise OnlyKingCanDo
        p = self.get_participant_by_id(identity)
        if p in self.current_team:
            self.current_team.remove(p)
        else:
            self.current_team.append(p)

    def confirm_team(self, participant: T):
        self.require_game_phase(GamePhase.TeamBuilding)
        if self.king != participant:
            raise OnlyKingCanDo
        if len(self.current_team) != self.step[1]:
            raise InvalidActionException('Please select correct number of team members')
        self.phase = GamePhase.TeamVote
        for p in self.participants:
            p.vote = None
        participant.vote = True

    def vote(self, participant: T, vote: bool):
        self.require_game_phase(GamePhase.TeamVote)
        participant.vote = None if (participant.vote is vote) else vote

    def process_vote_results(self) -> Optional[bool]:
        """
        Move to next phase if all votes are casted.
        The next phase is one of TeamBuilding, Quest or Finished
        :return: voting_result (bool) or None if voting is not completed
        """
        self.require_game_phase(GamePhase.TeamVote)
        if not all(p.vote is not None for p in self.participants):  # all-voted
            return
        is_voting_succeeded = sum(p.vote for p in self.participants) > (len(self.participants) / 2)
        if is_voting_succeeded:
            self.start_quest()
            self.failed_voting_count = 0
            return True
        self.failed_voting_count = getattr(self, 'failed_voting_count', 0) + 1
        if self.failed_voting_count >= len(self.participants):
            self.round_result.append(False)
            self.failed_voting_count = 0
            self.move_to_next_team_building()
        else:
            self.move_to_next_team_building()
        return False

    def move_to_next_team_building(self):
        self.phase = GamePhase.TeamBuilding
        self.current_team = []
        ps = self.participants
        self.king = ps[(ps.index(self.king) + 1) % len(ps)]

    def start_quest(self):
        self.phase = GamePhase.Quest
        for p in self.participants:
            p.quest_action = None

    def quest_action(self, participant: T, success: bool):
        self.require_game_phase(GamePhase.Quest)
        if participant not in self.current_team:
            raise InvalidActionException('You are not a member of this quest')
        participant.quest_action = None if (participant.quest_action is success) else success

    def process_quest_result(self) -> Optional[tuple[bool, int]]:
        """
        Move to next phase if all quest actions are casted.
        The next phase is one of TeamBuilding, Lady, GuessMerlin or Finished
        :return: None if quest is not completed
                is_quest_succeeded (bool), and number failed votes (int)
        """
        self.require_game_phase(GamePhase.Quest)
        if not all(p.quest_action is not None for p in self.current_team):  # all-voted
            return
        failed_votes = sum(not p.quest_action for p in self.current_team)
        is_quest_succeeded = failed_votes < self.step[0]
        self.round_result.append(is_quest_succeeded)
        if sum(not res for res in self.round_result) == 3:  # evil won
            self.finish(False)
        elif sum(res for res in self.round_result) == 3:  # servant won
            self.phase = GamePhase.GuessMerlin
        elif len(self.round_result) >= self.plan.lady_step and self.next_lady_candidates():
            self.phase = self.phase.Lady
        else:
            self.move_to_next_team_building()
        return is_quest_succeeded, failed_votes

    def next_lady_candidates(self):
        return [p for p in self.participants if p != self.lady and p not in self.past_ladies]

    def set_next_lady(self, participant: T, next_identity: str, dry_run=False) -> T:
        self.require_game_phase(GamePhase.Lady)
        if participant != self.lady:
            raise OnlyLadyCanDo
        next_lady = self.get_participant_by_id(next_identity)
        if next_lady not in self.next_lady_candidates():
            raise InvalidActionException('Cannot pass lady to: ' + str(next_lady))
        if not dry_run:
            self.past_ladies.append(self.lady)
            self.lady = next_lady
            self.move_to_next_team_building()
        return next_lady

    def guess_merlin(self, participant: T, identity: str, dry_run=False) -> T:
        self.require_game_phase(GamePhase.GuessMerlin)
        if participant != self.get_assassin():
            raise OnlyAssassinCanDo
        p = self.get_participant_by_id(identity)
        if p.role.is_evil:
            raise InvalidActionException('Evils cannot be merlin!')
        if not dry_run:
            self.finish(p.role is not Role.Merlin)
        return p

    def get_assassin(self) -> T:
        for p in self.participants:
            if p.role is Role.Assassin:
                return p
        for p in self.participants:
            if p.role.is_evil:
                return p

    def require_game_phase(self, phase: GamePhase):
        if self.phase != phase:
            raise exceptions.InvalidActionInThisPhase

    def finish(self, servant_won):
        self.phase = GamePhase.Finished
        self.game_result = servant_won

    @staticmethod
    def lock(game_id):
        return redis_client.lock(config.REDIS_PREFIX_GAME_LOCK + game_id, timeout=120)

    async def save(self):
        self.last_save = datetime.utcnow()
        await redis_client.set(REDIS_PREFIX_GAME + self.game_id, pickle.dumps(self))

    @classmethod
    async def load_by_id(cls, game_id: str) -> 'Game':
        value = await redis_client.get(config.REDIS_PREFIX_GAME + game_id)
        if value:
            return pickle.loads(value)

    async def delete(self):
        await redis_client.delete(config.REDIS_PREFIX_GAME + self.game_id)

    def get_participant_by_id(self, identity):
        for p in self.participants:
            if p.identity == identity:
                return p
        raise InvalidParticipant
